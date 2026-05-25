"""
strategy.py - ORB + Pullback Intraday Strategy Engine
======================================================

Implements the Opening Range Breakout strategy with pullback
confirmation for NIFTY 50 stocks.

State Machine (per stock)
-------------------------
1. WAITING_FIRST_CANDLE  (09:15–09:19) — aggregate ticks into 5-min ORB candle
2. ANALYZING_ORB         (09:20)       — validate price & range
3. WAITING_PULLBACK      (09:20–10:00) — wait for opposite-colour candle
4. WAITING_BREAKOUT      (after pullback) — tick-by-tick breakout detection
5. TRADE_ENTERED         — active trade, monitoring for 1:1 RR partial exit
6. PARTIAL_BOOKED        — 50 % booked, SL at cost, monitoring remainder
7. Terminal: STOPPED_OUT, SQUARED_OFF, REJECTED, EXPIRED

Design Notes
------------
* Every tick is routed through ``on_tick()`` which dispatches to the
  correct handler based on the stock's current state.
* Candle building is helper-based: ``_get_candle_slot()`` floors a
  datetime to the 5-minute boundary so we can detect slot transitions.
* **Max 1 trade per stock per day** — guarded by both the in-memory
  ``traded_today`` flag and a database check via ``db.has_traded_today()``.
* All timestamps use IST (``Asia/Kolkata``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from typing import Any

from zoneinfo import ZoneInfo

from broker import DhanBroker
from config import settings
from database import TradeDB
from notifier import TelegramNotifier
from risk_manager import RiskManager

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Timezone constant
# ---------------------------------------------------------------------------
IST = ZoneInfo("Asia/Kolkata")

# ---------------------------------------------------------------------------
# State constants
# ---------------------------------------------------------------------------
WAITING_FIRST_CANDLE = "WAITING_FIRST_CANDLE"
ANALYZING_ORB = "ANALYZING_ORB"
WAITING_PULLBACK = "WAITING_PULLBACK"
WAITING_BREAKOUT = "WAITING_BREAKOUT"
TRADE_ENTERED = "TRADE_ENTERED"
PARTIAL_BOOKED = "PARTIAL_BOOKED"
STOPPED_OUT = "STOPPED_OUT"
SQUARED_OFF = "SQUARED_OFF"
REJECTED = "REJECTED"
EXPIRED = "EXPIRED"

# Time boundaries (IST)
MARKET_OPEN = time(9, 15)
ORB_CANDLE_CLOSE = time(9, 20)
SCAN_CUTOFF = time(10, 0)
SQUARE_OFF_TIME = time(14, 30)
MARKET_CLOSE = time(15, 30)


# ---------------------------------------------------------------------------
# StockState dataclass
# ---------------------------------------------------------------------------
@dataclass
class StockState:
    """Per-stock state container tracking everything from ORB candle
    through trade lifecycle to final P&L."""

    # Identification
    security_id: int = 0
    symbol: str = ""
    exchange: str = "NSE_EQ"

    # State machine
    state: str = WAITING_FIRST_CANDLE

    # ORB candle (09:15–09:20)
    orb_open: float = 0.0
    orb_high: float = 0.0
    orb_low: float = 0.0
    orb_close: float = 0.0
    orb_range_pct: float = 0.0
    orb_is_green: bool = False

    # Pullback candle data
    pullback_high: float = 0.0
    pullback_low: float = 0.0
    pullback_detected: bool = False

    # Trade parameters
    entry_price: float = 0.0
    sl_price: float = 0.0
    target_1rr: float = 0.0
    target_2rr: float = 0.0
    quantity: int = 0
    remaining_qty: int = 0
    side: str = ""  # "BUY" or "SELL"

    # Order tracking
    order_id: str = ""
    sl_order_id: str = ""
    partial_order_id: str = ""

    # P&L
    trade_pnl: float = 0.0

    # Guards
    traded_today: bool = False
    trade_id: int = 0


# ---------------------------------------------------------------------------
# Candle dict helper
# ---------------------------------------------------------------------------
def _new_candle(ltp: float, ts: datetime) -> dict:
    """Create a fresh candle dict from the first tick."""
    return {
        "open": ltp,
        "high": ltp,
        "low": ltp,
        "close": ltp,
        "volume": 0,
        "start_time": ts,
    }


def _update_candle(candle: dict, ltp: float) -> None:
    """Update an existing candle dict with a new tick price."""
    candle["high"] = max(candle["high"], ltp)
    candle["low"] = min(candle["low"], ltp)
    candle["close"] = ltp


def _get_candle_slot(dt: datetime) -> datetime:
    """Floor *dt* to the nearest 5-minute boundary.

    Examples
    --------
    >>> _get_candle_slot(datetime(2025, 5, 26, 9, 17, 33))
    datetime(2025, 5, 26, 9, 15, 0)
    >>> _get_candle_slot(datetime(2025, 5, 26, 9, 20, 0))
    datetime(2025, 5, 26, 9, 20, 0)
    """
    minute_floor = (dt.minute // 5) * 5
    return dt.replace(minute=minute_floor, second=0, microsecond=0)


def _get_ist_now() -> datetime:
    """Return the current datetime in IST."""
    return datetime.now(tz=IST)


# ---------------------------------------------------------------------------
# Strategy engine
# ---------------------------------------------------------------------------
class ORBStrategyEngine:
    """Opening Range Breakout + Pullback strategy engine.

    Designed to be fed one tick at a time via ``on_tick()``.  All 50
    NIFTY stocks are tracked simultaneously, each with its own
    ``StockState`` instance.
    """

    def __init__(
        self,
        broker: DhanBroker,
        risk_mgr: RiskManager,
        db: TradeDB,
        notifier: TelegramNotifier,
        settings_obj: Any = None,
    ) -> None:
        self.broker: DhanBroker = broker
        self.risk_mgr: RiskManager = risk_mgr
        self.db: TradeDB = db
        self.notifier: TelegramNotifier = notifier
        self.settings = settings_obj or settings

        # Per-stock state keyed by security_id
        self.stocks: dict[int, StockState] = {}

        # Candle builders: security_id -> current partial candle dict
        self.candle_builder: dict[int, dict] = {}

        # Track the 5-min slot each builder is accumulating into
        self.current_candle_slot: dict[int, datetime] = {}

        logger.info("ORBStrategyEngine initialised")

    # ------------------------------------------------------------------
    # Day initialisation
    # ------------------------------------------------------------------

    def initialize_day(self, universe: list[dict]) -> None:
        """Reset all internal state for a new trading day.

        Parameters
        ----------
        universe : list[dict]
            List of dicts with keys ``security_id``, ``symbol``, and
            optionally ``exchange``.
        """
        self.stocks.clear()
        self.candle_builder.clear()
        self.current_candle_slot.clear()

        for stock in universe:
            sec_id = int(stock["security_id"])
            symbol = stock["symbol"]
            exchange = stock.get("exchange", "NSE_EQ")

            self.stocks[sec_id] = StockState(
                security_id=sec_id,
                symbol=symbol,
                exchange=exchange,
                state=WAITING_FIRST_CANDLE,
            )
            self.candle_builder[sec_id] = {}
            self.current_candle_slot[sec_id] = datetime.min.replace(tzinfo=IST)

        logger.info(
            "Day initialised: %d stocks in universe", len(self.stocks)
        )

    # ------------------------------------------------------------------
    # Main tick entry point
    # ------------------------------------------------------------------

    def on_tick(self, tick: dict) -> None:
        """Process a single live tick.

        This is the **main entry point** called by the market feed for
        every incoming price update.

        Parameters
        ----------
        tick : dict
            Normalised tick with at least ``security_id`` and ``LTP``.
        """
        sec_id = int(tick.get("security_id", 0))
        if sec_id not in self.stocks:
            return  # Not in our universe

        stock = self.stocks[sec_id]
        now = _get_ist_now()
        current_time = now.time()

        # Skip if in a terminal state
        if stock.state in (STOPPED_OUT, SQUARED_OFF, REJECTED, EXPIRED):
            return

        # Automatic 14:30 square-off check
        if current_time >= SQUARE_OFF_TIME and stock.state in (TRADE_ENTERED, PARTIAL_BOOKED):
            self._force_square_off_single(stock, tick, reason="14:30 auto square-off")
            return

        # Route to appropriate handler
        try:
            if stock.state == WAITING_FIRST_CANDLE:
                self._handle_first_candle_tick(sec_id, tick, now)
            elif stock.state == ANALYZING_ORB:
                self._analyze_orb(sec_id)
            elif stock.state == WAITING_PULLBACK:
                self._handle_pullback_tick(sec_id, tick, now)
            elif stock.state == WAITING_BREAKOUT:
                self._handle_breakout_tick(sec_id, tick, now)
            elif stock.state == TRADE_ENTERED:
                self._handle_trade_monitoring(sec_id, tick)
            elif stock.state == PARTIAL_BOOKED:
                self._handle_partial_monitoring(sec_id, tick)
        except Exception as exc:
            logger.error(
                "Error processing tick for %s (state=%s): %s",
                stock.symbol, stock.state, exc,
                exc_info=True,
            )
            self.notifier.error_alert(
                f"Tick processing error for {stock.symbol}: {exc}"
            )

    # ------------------------------------------------------------------
    # State handler: WAITING_FIRST_CANDLE
    # ------------------------------------------------------------------

    def _handle_first_candle_tick(
        self, sec_id: int, tick: dict, now: datetime
    ) -> None:
        """Aggregate ticks into the first 5-minute ORB candle (09:15–09:19).

        On the first tick at or after 09:20, the candle is finalised
        and the stock transitions to ANALYZING_ORB.
        """
        current_time = now.time()
        ltp = float(tick.get("LTP", 0.0))

        if ltp <= 0:
            return

        # If we've hit 09:20 or later, finalise the candle
        if current_time >= ORB_CANDLE_CLOSE:
            candle = self.candle_builder.get(sec_id)
            if candle:
                # Update one last time with this tick
                _update_candle(candle, ltp)
                self._finalise_orb_candle(sec_id, candle)
            else:
                # No ticks received during 09:15–09:19 — use this tick
                candle = _new_candle(ltp, now)
                self._finalise_orb_candle(sec_id, candle)
            return

        # Before 09:20 — build the candle
        if current_time < MARKET_OPEN:
            return  # Ignore pre-market ticks

        candle = self.candle_builder.get(sec_id)
        if not candle:
            # First tick for this stock
            self.candle_builder[sec_id] = _new_candle(ltp, now)
            logger.debug(
                "%s: ORB candle started at %.2f", self.stocks[sec_id].symbol, ltp
            )
        else:
            _update_candle(candle, ltp)

    def _finalise_orb_candle(self, sec_id: int, candle: dict) -> None:
        """Store ORB OHLC in the stock state and move to ANALYZING_ORB."""
        stock = self.stocks[sec_id]
        stock.orb_open = candle["open"]
        stock.orb_high = candle["high"]
        stock.orb_low = candle["low"]
        stock.orb_close = candle["close"]
        stock.orb_is_green = candle["close"] >= candle["open"]

        stock.state = ANALYZING_ORB

        logger.info(
            "%s: ORB candle finalised O=%.2f H=%.2f L=%.2f C=%.2f green=%s",
            stock.symbol, stock.orb_open, stock.orb_high,
            stock.orb_low, stock.orb_close, stock.orb_is_green,
        )

        # Log candle to database
        try:
            self.db.log_candle({
                "security_id": sec_id,
                "symbol": stock.symbol,
                "candle_type": "ORB",
                "open": stock.orb_open,
                "high": stock.orb_high,
                "low": stock.orb_low,
                "close": stock.orb_close,
                "timestamp": _get_ist_now().isoformat(),
            })
        except Exception as exc:
            logger.warning("Failed to log ORB candle for %s: %s", stock.symbol, exc)

        # Immediately analyse
        self._analyze_orb(sec_id)

    # ------------------------------------------------------------------
    # State handler: ANALYZING_ORB
    # ------------------------------------------------------------------

    def _analyze_orb(self, sec_id: int) -> None:
        """Validate the ORB candle and determine trade direction.

        Validation rules:
        1. ORB close price must be ≥ MIN_STOCK_PRICE (₹60).
        2. ORB range (high − low) / close must be ≤ MAX_RANGE_PCT (1.5 %).

        If valid, direction is set based on candle colour:
        * Green candle → BUY setup (look for red pullback, then buy on
          breakout above ORB high).
        * Red candle → SELL setup (look for green pullback, then sell on
          breakdown below ORB low).
        """
        stock = self.stocks[sec_id]
        orb_close = stock.orb_close
        orb_high = stock.orb_high
        orb_low = stock.orb_low

        # Validation 1: minimum price
        if orb_close < self.settings.MIN_STOCK_PRICE:
            stock.state = REJECTED
            logger.info(
                "%s REJECTED: close ₹%.2f < min ₹%.2f",
                stock.symbol, orb_close, self.settings.MIN_STOCK_PRICE,
            )
            self.db.log_system_event(
                "STOCK_REJECTED",
                f"{stock.symbol}: price {orb_close:.2f} below minimum {self.settings.MIN_STOCK_PRICE}",
            )
            return

        # Validation 2: range check
        valid, range_pct = self.risk_mgr.validate_range(orb_high, orb_low, orb_close)
        stock.orb_range_pct = range_pct

        if not valid:
            stock.state = REJECTED
            logger.info(
                "%s REJECTED: range %.2f%% > max %.2f%%",
                stock.symbol, range_pct, self.settings.MAX_RANGE_PCT,
            )
            self.db.log_system_event(
                "STOCK_REJECTED",
                f"{stock.symbol}: range {range_pct:.2f}% exceeds max {self.settings.MAX_RANGE_PCT}%",
            )
            return

        # Determine direction
        if stock.orb_is_green:
            stock.side = "BUY"
        else:
            stock.side = "SELL"

        stock.state = WAITING_PULLBACK

        logger.info(
            "%s SHORTLISTED: direction=%s range=%.2f%% O=%.2f H=%.2f L=%.2f C=%.2f",
            stock.symbol, stock.side, range_pct,
            stock.orb_open, orb_high, orb_low, orb_close,
        )

        # Notify and log
        self.notifier.stock_shortlisted(
            symbol=stock.symbol,
            orb_high=orb_high,
            orb_low=orb_low,
            range_pct=range_pct,
            direction=stock.side,
        )
        self.db.log_system_event(
            "STOCK_SHORTLISTED",
            f"{stock.symbol}: {stock.side} setup, range {range_pct:.2f}%",
        )

        # Reset candle builder for subsequent 5-min candles
        self.candle_builder[sec_id] = {}
        self.current_candle_slot[sec_id] = datetime.min.replace(tzinfo=IST)

    # ------------------------------------------------------------------
    # State handler: WAITING_PULLBACK
    # ------------------------------------------------------------------

    def _handle_pullback_tick(
        self, sec_id: int, tick: dict, now: datetime
    ) -> None:
        """Aggregate ticks into 5-min candles after the ORB, looking for
        a pullback candle (opposite colour to ORB).

        - BUY setup (ORB green): need a RED candle (close < open).
        - SELL setup (ORB red): need a GREEN candle (close > open).
        """
        stock = self.stocks[sec_id]
        current_time = now.time()
        ltp = float(tick.get("LTP", 0.0))

        if ltp <= 0:
            return

        # Expiry check
        if current_time >= SCAN_CUTOFF:
            stock.state = EXPIRED
            logger.info("%s: EXPIRED — no pullback before 10:00", stock.symbol)
            self.db.log_system_event(
                "STOCK_EXPIRED", f"{stock.symbol}: no pullback found before scan cutoff"
            )
            return

        # Determine the 5-min slot for this tick
        tick_slot = _get_candle_slot(now)
        prev_slot = self.current_candle_slot[sec_id]
        candle = self.candle_builder.get(sec_id)

        # First tick in this phase, or slot has advanced
        if not candle or (prev_slot != datetime.min.replace(tzinfo=IST) and tick_slot > prev_slot):
            # If there was a previous candle, finalise it
            if candle and candle.get("open") is not None:
                self._check_pullback_candle(sec_id, candle)
                if stock.state != WAITING_PULLBACK:
                    return  # Pullback found or state changed

            # Start a new candle
            self.candle_builder[sec_id] = _new_candle(ltp, now)
            self.current_candle_slot[sec_id] = tick_slot
        else:
            # Same slot — update the candle
            _update_candle(candle, ltp)
            self.current_candle_slot[sec_id] = tick_slot

    def _check_pullback_candle(self, sec_id: int, candle: dict) -> None:
        """Check whether a completed candle qualifies as a pullback.

        For a BUY setup the pullback candle must be RED (close < open).
        For a SELL setup the pullback candle must be GREEN (close > open).
        """
        stock = self.stocks[sec_id]
        c_open = candle["open"]
        c_close = candle["close"]
        c_high = candle["high"]
        c_low = candle["low"]
        is_green = c_close >= c_open

        logger.debug(
            "%s: 5-min candle closed O=%.2f H=%.2f L=%.2f C=%.2f green=%s",
            stock.symbol, c_open, c_high, c_low, c_close, is_green,
        )

        # Log the candle
        try:
            self.db.log_candle({
                "security_id": sec_id,
                "symbol": stock.symbol,
                "candle_type": "PULLBACK_CHECK",
                "open": c_open,
                "high": c_high,
                "low": c_low,
                "close": c_close,
                "timestamp": _get_ist_now().isoformat(),
            })
        except Exception as exc:
            logger.warning("Failed to log pullback candle for %s: %s", stock.symbol, exc)

        pullback_found = False
        if stock.side == "BUY" and not is_green:
            # Red candle in a bullish setup -> pullback ✓
            pullback_found = True
        elif stock.side == "SELL" and is_green:
            # Green candle in a bearish setup -> pullback ✓
            pullback_found = True

        if pullback_found:
            stock.pullback_high = c_high
            stock.pullback_low = c_low
            stock.pullback_detected = True
            stock.state = WAITING_BREAKOUT

            logger.info(
                "%s: PULLBACK DETECTED (side=%s) candle O=%.2f C=%.2f PB_H=%.2f PB_L=%.2f",
                stock.symbol, stock.side, c_open, c_close, c_high, c_low,
            )
            self.db.log_system_event(
                "PULLBACK_DETECTED",
                f"{stock.symbol}: {stock.side} pullback H={c_high:.2f} L={c_low:.2f}",
            )

    # ------------------------------------------------------------------
    # State handler: WAITING_BREAKOUT
    # ------------------------------------------------------------------

    def _handle_breakout_tick(
        self, sec_id: int, tick: dict, now: datetime
    ) -> None:
        """Check **every tick** for a breakout of the ORB high/low.

        - BUY: LTP > ORB high → execute buy.
        - SELL: LTP < ORB low → execute short.
        """
        stock = self.stocks[sec_id]
        current_time = now.time()
        ltp = float(tick.get("LTP", 0.0))

        if ltp <= 0:
            return

        # Expiry check
        if current_time >= SCAN_CUTOFF:
            stock.state = EXPIRED
            logger.info("%s: EXPIRED — no breakout before 10:00", stock.symbol)
            self.db.log_system_event(
                "STOCK_EXPIRED", f"{stock.symbol}: no breakout before scan cutoff"
            )
            return

        # Guard: max 1 trade per stock per day
        if stock.traded_today:
            stock.state = REJECTED
            logger.info("%s: Already traded today — skipping", stock.symbol)
            return

        breakout = False
        if stock.side == "BUY" and ltp > stock.orb_high:
            breakout = True
        elif stock.side == "SELL" and ltp < stock.orb_low:
            breakout = True

        if breakout:
            self._execute_entry(sec_id, ltp, now)

    def _execute_entry(self, sec_id: int, ltp: float, now: datetime) -> None:
        """Execute the trade entry on breakout confirmation.

        Steps:
        1. Double-check ``traded_today`` and ``db.has_traded_today()``.
        2. Calculate quantity via risk manager.
        3. Place market order via broker.
        4. Set SL = pullback_low (BUY) or pullback_high (SELL).
        5. Calculate 1RR and 2RR targets.
        6. Log to database & send Telegram alert.
        7. Transition to TRADE_ENTERED.
        """
        stock = self.stocks[sec_id]

        # Double-check: max 1 trade per stock
        if stock.traded_today or self.db.has_traded_today(sec_id):
            stock.state = REJECTED
            stock.traded_today = True
            logger.info("%s: Already traded today (DB check) — aborting entry", stock.symbol)
            return

        # Determine SL
        if stock.side == "BUY":
            sl_price = stock.pullback_low
        else:
            sl_price = stock.pullback_high

        # Safety: SL must not equal entry
        if abs(ltp - sl_price) < 0.05:
            stock.state = REJECTED
            logger.warning(
                "%s: SL too close to entry (entry=%.2f, sl=%.2f) — skipping",
                stock.symbol, ltp, sl_price,
            )
            self.db.log_system_event(
                "TRADE_REJECTED",
                f"{stock.symbol}: SL too close entry={ltp:.2f} sl={sl_price:.2f}",
            )
            return

        # Calculate quantity
        qty = self.risk_mgr.calculate_quantity(ltp)
        if qty <= 0:
            stock.state = REJECTED
            logger.warning("%s: Calculated quantity is 0 — insufficient capital", stock.symbol)
            self.db.log_system_event(
                "TRADE_REJECTED", f"{stock.symbol}: qty=0, insufficient capital at price {ltp:.2f}"
            )
            return

        # Calculate targets
        target_1rr, target_2rr = self.risk_mgr.calculate_targets(ltp, sl_price, stock.side)

        logger.info(
            "%s: BREAKOUT %s @ %.2f | SL=%.2f | T1=%.2f | T2=%.2f | qty=%d",
            stock.symbol, stock.side, ltp, sl_price, target_1rr, target_2rr, qty,
        )

        # Place market order
        try:
            order_resp = self.broker.place_market_order(
                security_id=sec_id,
                exchange=stock.exchange,
                qty=qty,
                side=stock.side,
            )
        except Exception as exc:
            logger.error("%s: Order placement failed: %s", stock.symbol, exc)
            self.notifier.error_alert(f"Order failed for {stock.symbol}: {exc}")
            stock.state = REJECTED
            return

        order_id = ""
        resp_data = order_resp.get("data", {})
        if isinstance(resp_data, dict):
            order_id = str(resp_data.get("orderId", ""))

        if not order_id:
            logger.error("%s: No orderId in response: %s", stock.symbol, order_resp)
            self.notifier.error_alert(f"No orderId for {stock.symbol}: {order_resp}")
            stock.state = REJECTED
            return

        # Update stock state
        stock.entry_price = ltp
        stock.sl_price = sl_price
        stock.target_1rr = target_1rr
        stock.target_2rr = target_2rr
        stock.quantity = qty
        stock.remaining_qty = qty
        stock.order_id = order_id
        stock.traded_today = True
        stock.state = TRADE_ENTERED

        # Log to database
        try:
            trade_record = {
                "security_id": sec_id,
                "symbol": stock.symbol,
                "side": stock.side,
                "entry_price": ltp,
                "sl_price": sl_price,
                "target_1rr": target_1rr,
                "target_2rr": target_2rr,
                "quantity": qty,
                "remaining_qty": qty,
                "order_id": order_id,
                "status": "ENTERED",
                "orb_open": stock.orb_open,
                "orb_high": stock.orb_high,
                "orb_low": stock.orb_low,
                "orb_close": stock.orb_close,
                "orb_range_pct": stock.orb_range_pct,
                "pullback_high": stock.pullback_high,
                "pullback_low": stock.pullback_low,
                "entry_time": now.isoformat(),
            }
            stock.trade_id = self.db.log_trade(trade_record)
        except Exception as exc:
            logger.error("Failed to log trade for %s: %s", stock.symbol, exc)

        # Telegram notification
        try:
            self.notifier.trade_executed(
                symbol=stock.symbol,
                side=stock.side,
                qty=qty,
                entry=ltp,
                sl=sl_price,
                target=target_1rr,
            )
        except Exception as exc:
            logger.warning("Telegram notification failed for %s: %s", stock.symbol, exc)

        logger.info(
            "%s: TRADE ENTERED — order_id=%s, trade_id=%d",
            stock.symbol, order_id, stock.trade_id,
        )

    # ------------------------------------------------------------------
    # State handler: TRADE_ENTERED
    # ------------------------------------------------------------------

    def _handle_trade_monitoring(self, sec_id: int, tick: dict) -> None:
        """Monitor an active trade for SL hit or 1:1 RR target.

        * **SL hit**: exit the full position at market, mark STOPPED_OUT.
        * **1:1 RR hit**: book 50 % at market, move SL to entry (cost-to-
          cost), transition to PARTIAL_BOOKED.
        """
        stock = self.stocks[sec_id]
        ltp = float(tick.get("LTP", 0.0))

        if ltp <= 0:
            return

        # --- Check Stop Loss ---
        sl_hit = False
        if stock.side == "BUY" and ltp <= stock.sl_price:
            sl_hit = True
        elif stock.side == "SELL" and ltp >= stock.sl_price:
            sl_hit = True

        if sl_hit:
            self._exit_trade(
                stock=stock,
                exit_price=ltp,
                exit_qty=stock.remaining_qty,
                reason="SL Hit",
                new_state=STOPPED_OUT,
            )
            return

        # --- Check 1:1 RR Target ---
        target_hit = False
        if stock.side == "BUY" and ltp >= stock.target_1rr:
            target_hit = True
        elif stock.side == "SELL" and ltp <= stock.target_1rr:
            target_hit = True

        if target_hit:
            self._book_partial_profit(stock, ltp)

    def _book_partial_profit(self, stock: StockState, ltp: float) -> None:
        """Book 50 % of the position at 1:1 RR and move SL to entry."""
        partial_qty = self.risk_mgr.partial_book_qty(stock.quantity)
        if partial_qty <= 0:
            partial_qty = stock.remaining_qty  # Exit all if qty too small

        # Determine exit side (opposite of entry)
        exit_side = "SELL" if stock.side == "BUY" else "BUY"

        logger.info(
            "%s: 1RR TARGET HIT @ %.2f — booking %d/%d shares",
            stock.symbol, ltp, partial_qty, stock.remaining_qty,
        )

        # Place partial exit order
        try:
            partial_resp = self.broker.place_market_order(
                security_id=stock.security_id,
                exchange=stock.exchange,
                qty=partial_qty,
                side=exit_side,
            )
            resp_data = partial_resp.get("data", {})
            stock.partial_order_id = str(resp_data.get("orderId", "")) if isinstance(resp_data, dict) else ""
        except Exception as exc:
            logger.error("%s: Partial profit order failed: %s", stock.symbol, exc)
            self.notifier.error_alert(f"Partial order failed for {stock.symbol}: {exc}")
            return  # Stay in TRADE_ENTERED, try again on next tick

        # Move SL to entry (cost-to-cost / breakeven)
        old_sl = stock.sl_price
        stock.sl_price = stock.entry_price
        stock.remaining_qty -= partial_qty

        # Calculate booked P&L for the partial
        if stock.side == "BUY":
            partial_pnl = (ltp - stock.entry_price) * partial_qty
        else:
            partial_pnl = (stock.entry_price - ltp) * partial_qty
        stock.trade_pnl += partial_pnl

        stock.state = PARTIAL_BOOKED

        logger.info(
            "%s: PARTIAL BOOKED — %d shares @ %.2f, P&L=₹%.2f, new SL=%.2f (entry), remaining=%d",
            stock.symbol, partial_qty, ltp, partial_pnl, stock.sl_price, stock.remaining_qty,
        )

        # Update database
        try:
            self.db.update_trade(stock.trade_id, {
                "status": "PARTIAL_BOOKED",
                "partial_qty": partial_qty,
                "partial_price": ltp,
                "partial_pnl": partial_pnl,
                "remaining_qty": stock.remaining_qty,
                "sl_price": stock.sl_price,
                "partial_order_id": stock.partial_order_id,
            })
        except Exception as exc:
            logger.warning("Failed to update trade for %s: %s", stock.symbol, exc)

        # Telegram notification
        try:
            self.notifier.partial_profit(
                symbol=stock.symbol,
                booked_qty=partial_qty,
                booked_price=ltp,
                new_sl=stock.sl_price,
            )
        except Exception as exc:
            logger.warning("Telegram partial notification failed for %s: %s", stock.symbol, exc)

    # ------------------------------------------------------------------
    # State handler: PARTIAL_BOOKED
    # ------------------------------------------------------------------

    def _handle_partial_monitoring(self, sec_id: int, tick: dict) -> None:
        """Monitor the remaining position after partial booking.

        * **SL hit** (at entry = breakeven): exit remaining, STOPPED_OUT.
        * **2:1 RR hit**: exit remaining with full profit, SQUARED_OFF.
        """
        stock = self.stocks[sec_id]
        ltp = float(tick.get("LTP", 0.0))

        if ltp <= 0:
            return

        # --- Check SL (now at entry / breakeven) ---
        sl_hit = False
        if stock.side == "BUY" and ltp <= stock.sl_price:
            sl_hit = True
        elif stock.side == "SELL" and ltp >= stock.sl_price:
            sl_hit = True

        if sl_hit:
            self._exit_trade(
                stock=stock,
                exit_price=ltp,
                exit_qty=stock.remaining_qty,
                reason="SL Hit (breakeven)",
                new_state=STOPPED_OUT,
            )
            return

        # --- Check 2:1 RR Target ---
        target_hit = False
        if stock.side == "BUY" and ltp >= stock.target_2rr:
            target_hit = True
        elif stock.side == "SELL" and ltp <= stock.target_2rr:
            target_hit = True

        if target_hit:
            self._exit_trade(
                stock=stock,
                exit_price=ltp,
                exit_qty=stock.remaining_qty,
                reason="2RR Target Hit",
                new_state=SQUARED_OFF,
            )

    # ------------------------------------------------------------------
    # Trade exit helper
    # ------------------------------------------------------------------

    def _exit_trade(
        self,
        stock: StockState,
        exit_price: float,
        exit_qty: int,
        reason: str,
        new_state: str,
    ) -> None:
        """Place an exit order and finalise the trade.

        Parameters
        ----------
        stock : StockState
            The stock being exited.
        exit_price : float
            The price at which the exit is triggered.
        exit_qty : int
            Number of shares to exit.
        reason : str
            Human-readable exit reason.
        new_state : str
            Terminal state to transition to.
        """
        exit_side = "SELL" if stock.side == "BUY" else "BUY"

        logger.info(
            "%s: EXITING %s — %d shares @ %.2f reason='%s'",
            stock.symbol, exit_side, exit_qty, exit_price, reason,
        )

        # Place exit market order
        try:
            self.broker.place_market_order(
                security_id=stock.security_id,
                exchange=stock.exchange,
                qty=exit_qty,
                side=exit_side,
            )
        except Exception as exc:
            logger.error(
                "%s: Exit order failed: %s — MANUAL INTERVENTION NEEDED",
                stock.symbol, exc,
            )
            self.notifier.error_alert(
                f"⚠️ EXIT ORDER FAILED for {stock.symbol}: {exc}. Manual exit required!"
            )
            # Don't change state — keep trying on subsequent ticks
            return

        # Calculate P&L for this exit leg
        if stock.side == "BUY":
            leg_pnl = (exit_price - stock.entry_price) * exit_qty
        else:
            leg_pnl = (stock.entry_price - exit_price) * exit_qty

        stock.trade_pnl += leg_pnl
        stock.remaining_qty -= exit_qty
        stock.state = new_state

        logger.info(
            "%s: %s — leg P&L=₹%.2f, total P&L=₹%.2f",
            stock.symbol, new_state, leg_pnl, stock.trade_pnl,
        )

        # Update database
        try:
            self.db.update_trade(stock.trade_id, {
                "status": new_state,
                "exit_price": exit_price,
                "exit_reason": reason,
                "pnl": stock.trade_pnl,
                "remaining_qty": stock.remaining_qty,
                "exit_time": _get_ist_now().isoformat(),
            })
        except Exception as exc:
            logger.warning("Failed to update trade on exit for %s: %s", stock.symbol, exc)

        # Telegram notification
        try:
            self.notifier.trade_closed(
                symbol=stock.symbol,
                exit_price=exit_price,
                pnl=stock.trade_pnl,
                reason=reason,
            )
        except Exception as exc:
            logger.warning("Telegram close notification failed for %s: %s", stock.symbol, exc)

    # ------------------------------------------------------------------
    # Force square-off (14:30)
    # ------------------------------------------------------------------

    def force_square_off_all(self) -> None:
        """Close ALL open positions at 14:30 (or on manual trigger).

        Iterates over every stock in TRADE_ENTERED or PARTIAL_BOOKED
        and places exit market orders.
        """
        logger.info("=== FORCE SQUARE-OFF ALL ===")
        active = self.get_active_trades()

        if not active:
            logger.info("No active trades to square off")
            return

        for stock in active:
            # Fetch the latest LTP for accurate P&L
            try:
                ltp = self.broker.get_ltp(stock.security_id)
            except Exception:
                ltp = stock.entry_price  # Fallback

            self._force_square_off_single(
                stock, {"LTP": ltp, "security_id": stock.security_id},
                reason="14:30 auto square-off",
            )

        logger.info("Force square-off complete: %d trades closed", len(active))

    def _force_square_off_single(
        self, stock: StockState, tick: dict, reason: str = "14:30 auto square-off"
    ) -> None:
        """Force-close a single stock position."""
        if stock.remaining_qty <= 0:
            stock.state = SQUARED_OFF
            return

        ltp = float(tick.get("LTP", stock.entry_price))

        self._exit_trade(
            stock=stock,
            exit_price=ltp,
            exit_qty=stock.remaining_qty,
            reason=reason,
            new_state=SQUARED_OFF,
        )

    # ------------------------------------------------------------------
    # Query methods
    # ------------------------------------------------------------------

    def get_all_states(self) -> dict[int, StockState]:
        """Return all stock states (for dashboard / monitoring)."""
        return dict(self.stocks)

    def get_active_trades(self) -> list[StockState]:
        """Return stocks currently in TRADE_ENTERED or PARTIAL_BOOKED."""
        return [
            s for s in self.stocks.values()
            if s.state in (TRADE_ENTERED, PARTIAL_BOOKED)
        ]

    def get_daily_summary(self) -> dict:
        """Compute end-of-day summary statistics.

        Returns
        -------
        dict
            Keys: total_pnl, trades_count, wins, losses, breakevens,
            active_count, rejected_count, expired_count.
        """
        total_pnl: float = 0.0
        trades_count: int = 0
        wins: int = 0
        losses: int = 0
        breakevens: int = 0
        active_count: int = 0
        rejected_count: int = 0
        expired_count: int = 0

        for stock in self.stocks.values():
            if stock.state in (TRADE_ENTERED, PARTIAL_BOOKED):
                active_count += 1
            elif stock.state == REJECTED:
                rejected_count += 1
            elif stock.state == EXPIRED:
                expired_count += 1
            elif stock.state in (STOPPED_OUT, SQUARED_OFF):
                trades_count += 1
                total_pnl += stock.trade_pnl
                if stock.trade_pnl > 0.50:
                    wins += 1
                elif stock.trade_pnl < -0.50:
                    losses += 1
                else:
                    breakevens += 1

        summary = {
            "total_pnl": round(total_pnl, 2),
            "trades_count": trades_count,
            "wins": wins,
            "losses": losses,
            "breakevens": breakevens,
            "active_count": active_count,
            "rejected_count": rejected_count,
            "expired_count": expired_count,
        }

        logger.info(
            "Daily summary: P&L=₹%.2f, trades=%d, W=%d, L=%d, BE=%d",
            total_pnl, trades_count, wins, losses, breakevens,
        )
        return summary

    # ------------------------------------------------------------------
    # State persistence (for recovery)
    # ------------------------------------------------------------------

    def save_states(self) -> None:
        """Persist all non-terminal stock states to the database for
        crash recovery."""
        for stock in self.stocks.values():
            if stock.state in (WAITING_FIRST_CANDLE, REJECTED, EXPIRED):
                continue
            try:
                state_dict = {
                    "security_id": stock.security_id,
                    "symbol": stock.symbol,
                    "exchange": stock.exchange,
                    "state": stock.state,
                    "orb_open": stock.orb_open,
                    "orb_high": stock.orb_high,
                    "orb_low": stock.orb_low,
                    "orb_close": stock.orb_close,
                    "orb_range_pct": stock.orb_range_pct,
                    "orb_is_green": stock.orb_is_green,
                    "pullback_high": stock.pullback_high,
                    "pullback_low": stock.pullback_low,
                    "pullback_detected": stock.pullback_detected,
                    "entry_price": stock.entry_price,
                    "sl_price": stock.sl_price,
                    "target_1rr": stock.target_1rr,
                    "target_2rr": stock.target_2rr,
                    "quantity": stock.quantity,
                    "remaining_qty": stock.remaining_qty,
                    "side": stock.side,
                    "order_id": stock.order_id,
                    "partial_order_id": stock.partial_order_id,
                    "trade_pnl": stock.trade_pnl,
                    "traded_today": stock.traded_today,
                    "trade_id": stock.trade_id,
                    "timestamp": _get_ist_now().isoformat(),
                }
                self.db.save_stock_state(state_dict)
            except Exception as exc:
                logger.error("Failed to save state for %s: %s", stock.symbol, exc)

    def restore_states(self) -> None:
        """Restore stock states from the database after a crash/restart."""
        try:
            saved_states = self.db.load_stock_states()
        except Exception as exc:
            logger.error("Failed to load saved states: %s", exc)
            return

        restored = 0
        for state_dict in saved_states:
            sec_id = int(state_dict.get("security_id", 0))
            if sec_id not in self.stocks:
                continue

            stock = self.stocks[sec_id]
            stock.state = state_dict.get("state", stock.state)
            stock.orb_open = float(state_dict.get("orb_open", 0))
            stock.orb_high = float(state_dict.get("orb_high", 0))
            stock.orb_low = float(state_dict.get("orb_low", 0))
            stock.orb_close = float(state_dict.get("orb_close", 0))
            stock.orb_range_pct = float(state_dict.get("orb_range_pct", 0))
            stock.orb_is_green = bool(state_dict.get("orb_is_green", False))
            stock.pullback_high = float(state_dict.get("pullback_high", 0))
            stock.pullback_low = float(state_dict.get("pullback_low", 0))
            stock.pullback_detected = bool(state_dict.get("pullback_detected", False))
            stock.entry_price = float(state_dict.get("entry_price", 0))
            stock.sl_price = float(state_dict.get("sl_price", 0))
            stock.target_1rr = float(state_dict.get("target_1rr", 0))
            stock.target_2rr = float(state_dict.get("target_2rr", 0))
            stock.quantity = int(state_dict.get("quantity", 0))
            stock.remaining_qty = int(state_dict.get("remaining_qty", 0))
            stock.side = state_dict.get("side", "")
            stock.order_id = state_dict.get("order_id", "")
            stock.partial_order_id = state_dict.get("partial_order_id", "")
            stock.trade_pnl = float(state_dict.get("trade_pnl", 0))
            stock.traded_today = bool(state_dict.get("traded_today", False))
            stock.trade_id = int(state_dict.get("trade_id", 0))
            restored += 1

        logger.info("Restored %d stock states from database", restored)
