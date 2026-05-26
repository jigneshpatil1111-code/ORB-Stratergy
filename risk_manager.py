"""
ORB Intraday Trading System - Risk Manager
============================================
Centralised position-sizing, target calculation, and range validation.

All monetary values are in INR (₹).  Quantities are whole shares.
"""

import logging
import math

from config import settings

logger = logging.getLogger(__name__)


class RiskManager:
    """
    Manages position sizing and risk-reward calculations for the ORB
    strategy.

    Attributes:
        base_capital: The raw capital available (before leverage).
        leverage:     Multiplier applied to base capital.
    """

    def __init__(
        self,
        base_capital: float | None = None,
        leverage: int | None = None,
    ) -> None:
        """
        Initialise the risk manager.

        Args:
            base_capital: Base capital in INR.  Defaults to ``settings.BASE_CAPITAL``.
            leverage:     Leverage multiplier.   Defaults to ``settings.LEVERAGE``.
        """
        self.base_capital: float = (
            base_capital if base_capital is not None else settings.BASE_CAPITAL
        )
        self.leverage: int = (
            leverage if leverage is not None else settings.LEVERAGE
        )
        logger.info(
            "RiskManager initialised: capital=₹%s, leverage=%dx, "
            "effective=₹%s",
            f"{self.base_capital:,.0f}",
            self.leverage,
            f"{self.effective_capital:,.0f}",
        )

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def effective_capital(self) -> float:
        """Total deployable capital after applying leverage."""
        return self.base_capital * self.leverage

    # ------------------------------------------------------------------
    # Position sizing
    # ------------------------------------------------------------------

    def calculate_quantity(self, entry_price: float) -> int:
        """
        Compute the number of shares to buy/sell.

        Formula::

            qty = floor(effective_capital / entry_price)

        Args:
            entry_price: Expected entry price per share (₹).

        Returns:
            Integer quantity (≥ 0).  Returns 0 if the entry price is
            invalid or too high for the available capital.
        """
        if entry_price <= 0:
            logger.warning("Invalid entry price: %.2f – returning qty 0", entry_price)
            return 0

        qty = math.floor(self.effective_capital / entry_price)
        qty = max(qty, 0)

        # Enforce per-trade quantity cap (for safe live testing)
        max_qty = getattr(settings, "MAX_QTY_PER_TRADE", 0)
        if max_qty > 0 and qty > max_qty:
            logger.info(
                "Quantity capped: calculated=%d, max_allowed=%d",
                qty, max_qty,
            )
            qty = max_qty

        logger.debug(
            "Quantity calc: ₹%.2f capital / ₹%.2f price = %d shares (max_per_trade=%d)",
            self.effective_capital,
            entry_price,
            qty,
            max_qty,
        )
        return qty

    # ------------------------------------------------------------------
    # Target calculation
    # ------------------------------------------------------------------

    def calculate_targets(
        self, entry: float, sl: float, side: str
    ) -> tuple[float, float]:
        """
        Compute 1-RR and 2-RR target prices.

        For a **BUY** trade::

            risk       = entry - sl
            target_1rr = entry + risk      (1× reward)
            target_2rr = entry + 2 × risk  (2× reward)

        For a **SELL** trade::

            risk       = sl - entry
            target_1rr = entry - risk
            target_2rr = entry - 2 × risk

        Args:
            entry: Entry price.
            sl:    Stop-loss price.
            side:  ``"BUY"`` or ``"SELL"``.

        Returns:
            Tuple of ``(target_1rr, target_2rr)``, both rounded to 2
            decimal places.

        Raises:
            ValueError: If *side* is not ``BUY`` or ``SELL``, or if the
                        risk computes to ≤ 0.
        """
        side_upper = side.upper()

        if side_upper == "BUY":
            risk = entry - sl
        elif side_upper == "SELL":
            risk = sl - entry
        else:
            raise ValueError(f"Invalid side '{side}' – expected BUY or SELL")

        if risk <= 0:
            raise ValueError(
                f"Non-positive risk ({risk:.2f}) for entry={entry}, "
                f"sl={sl}, side={side_upper}"
            )

        if side_upper == "BUY":
            target_1rr = round(entry + risk, 2)
            target_2rr = round(entry + 2 * risk, 2)
        else:
            target_1rr = round(entry - risk, 2)
            target_2rr = round(entry - 2 * risk, 2)

        logger.debug(
            "Targets (%s): entry=%.2f, sl=%.2f, risk=%.2f → "
            "T1=%.2f, T2=%.2f",
            side_upper, entry, sl, risk, target_1rr, target_2rr,
        )
        return target_1rr, target_2rr

    # ------------------------------------------------------------------
    # Partial profit booking
    # ------------------------------------------------------------------

    def partial_book_qty(self, total_qty: int) -> int:
        """
        Calculate the quantity to book at the first target (1-RR).

        Default is 50% of the position (configurable via
        ``settings.PARTIAL_BOOK_PCT``).

        Args:
            total_qty: Original full position size.

        Returns:
            Integer quantity to book.  Always ≥ 1 if total_qty > 1,
            otherwise equals total_qty.
        """
        if total_qty <= 1:
            return total_qty

        book_qty = math.floor(total_qty * settings.PARTIAL_BOOK_PCT)
        # Ensure we book at least 1 share
        book_qty = max(book_qty, 1)
        logger.debug(
            "Partial book: %d of %d shares (%.0f%%)",
            book_qty, total_qty, settings.PARTIAL_BOOK_PCT * 100,
        )
        return book_qty

    # ------------------------------------------------------------------
    # Range validation
    # ------------------------------------------------------------------

    def validate_range(
        self, high: float, low: float, close: float
    ) -> tuple[bool, float]:
        """
        Check whether the ORB candle range is within the acceptable
        threshold.

        Formula::

            range_pct = ((high - low) / close) × 100

        The range is valid if ``range_pct <= settings.MAX_RANGE_PCT``.

        Args:
            high:  ORB candle high price.
            low:   ORB candle low price.
            close: ORB candle close price.

        Returns:
            Tuple of ``(is_valid, range_pct)``.  ``is_valid`` is True
            when the range percentage is within the configured maximum.
        """
        if close <= 0:
            logger.warning("Invalid close price: %.2f", close)
            return False, 0.0

        range_pct = round(((high - low) / close) * 100, 4)
        is_valid = range_pct <= settings.MAX_RANGE_PCT
        logger.debug(
            "Range validation: high=%.2f, low=%.2f, close=%.2f → "
            "%.2f%% (max=%.2f%%) → %s",
            high, low, close, range_pct, settings.MAX_RANGE_PCT,
            "PASS" if is_valid else "REJECT",
        )
        return is_valid, range_pct

    # ------------------------------------------------------------------
    # Price validation
    # ------------------------------------------------------------------

    def validate_min_price(self, price: float) -> bool:
        """
        Reject stocks below the minimum price threshold.

        This filters out penny stocks and illiquid scrips that tend
        to have erratic ORB candles.

        Args:
            price: Current / close price of the stock.

        Returns:
            True if the price meets or exceeds the minimum.
        """
        is_valid = price >= settings.MIN_STOCK_PRICE
        if not is_valid:
            logger.debug(
                "Price ₹%.2f below minimum ₹%.2f – rejected",
                price, settings.MIN_STOCK_PRICE,
            )
        return is_valid

    def validate_max_price(self, price: float) -> bool:
        """
        Reject stocks above the maximum price threshold.

        Args:
            price: Current / close price of the stock.

        Returns:
            True if the price is within the maximum limit.
        """
        is_valid = price <= settings.MAX_STOCK_PRICE
        if not is_valid:
            logger.debug(
                "Price ₹%.2f above maximum ₹%.2f – rejected",
                price, settings.MAX_STOCK_PRICE,
            )
        return is_valid

