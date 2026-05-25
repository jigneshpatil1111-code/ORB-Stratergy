"""
ORB Intraday Trading System - Telegram Notifier
=================================================
Sends formatted HTML notifications to a Telegram chat via the Bot API.
Uses synchronous ``requests`` to avoid async dependency in the trading
engine's hot path.  All methods are fail-safe — a notification failure
must never crash the main system.
"""

import logging
import time as _time
from datetime import datetime

import requests

from config import IST

logger = logging.getLogger(__name__)

# Telegram Bot API base URL
_TG_API = "https://api.telegram.org/bot{token}/sendMessage"

# Retry parameters
_MAX_RETRIES = 3
_RETRY_DELAY_S = 1.0


class TelegramNotifier:
    """
    Sends rich HTML-formatted messages to a Telegram chat.

    All public methods are **fail-safe**: they catch every exception
    internally and log a warning instead of propagating.  This ensures
    the trading engine is never disrupted by a Telegram outage.
    """

    def __init__(self, bot_token: str, chat_id: str) -> None:
        """
        Args:
            bot_token: Telegram Bot API token (from @BotFather).
            chat_id:   Target chat / group / channel ID.
        """
        self._bot_token = bot_token
        self._chat_id = chat_id
        self._enabled = bool(bot_token and chat_id)

        if not self._enabled:
            logger.warning(
                "TelegramNotifier disabled – bot_token or chat_id is empty"
            )

    # ------------------------------------------------------------------
    # Core send
    # ------------------------------------------------------------------

    def send(self, message: str) -> None:
        """
        Send *message* to Telegram with HTML parse mode.

        Retries up to 3 times with a 1-second delay between attempts.
        Never raises — logs warnings on failure.

        Args:
            message: HTML-formatted message text.
        """
        if not self._enabled:
            logger.debug("Telegram disabled – message suppressed")
            return

        url = _TG_API.format(token=self._bot_token)
        payload = {
            "chat_id": self._chat_id,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }

        last_err: Exception | None = None
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                resp = requests.post(url, json=payload, timeout=10)
                if resp.status_code == 200 and resp.json().get("ok"):
                    logger.debug("Telegram message sent (attempt %d)", attempt)
                    return
                # Non-OK response
                logger.warning(
                    "Telegram API error (attempt %d/%d): %s",
                    attempt, _MAX_RETRIES, resp.text[:300],
                )
            except requests.RequestException as exc:
                last_err = exc
                logger.warning(
                    "Telegram request failed (attempt %d/%d): %s",
                    attempt, _MAX_RETRIES, exc,
                )

            if attempt < _MAX_RETRIES:
                _time.sleep(_RETRY_DELAY_S)

        logger.error(
            "Telegram send failed after %d retries – message dropped. "
            "Last error: %s",
            _MAX_RETRIES, last_err,
        )

    # ------------------------------------------------------------------
    # Pre-formatted messages
    # ------------------------------------------------------------------

    def system_start(self) -> None:
        """Notify that the ORB system has started."""
        now = datetime.now(IST)
        from config import settings  # deferred to avoid circular import at module level

        msg = (
            "🚀 <b>ORB Trading System Started</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📅 Date    : <code>{now.strftime('%d-%b-%Y (%A)')}</code>\n"
            f"🕘 Time    : <code>{now.strftime('%H:%M:%S')} IST</code>\n"
            f"📝 Mode    : <b>{settings.mode_label}</b>\n"
            f"💰 Capital : <code>₹{settings.effective_capital:,.0f}</code> "
            f"({settings.LEVERAGE}x leverage)\n"
            f"📊 Range   : <code>≤ {settings.MAX_RANGE_PCT}%</code>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━"
        )
        self.send(msg)

    def stock_shortlisted(
        self,
        symbol: str,
        orb_high: float,
        orb_low: float,
        range_pct: float,
        direction: str,
    ) -> None:
        """
        Notify that a stock passed the ORB filter.

        Args:
            symbol:    NSE trading symbol.
            orb_high:  ORB candle high.
            orb_low:   ORB candle low.
            range_pct: ORB range as a percentage.
            direction: Expected trade direction (BUY / SELL).
        """
        arrow = "🟢 BUY" if direction.upper() == "BUY" else "🔴 SELL"
        msg = (
            f"📋 <b>Stock Shortlisted</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🏷️ Symbol    : <b>{symbol}</b>\n"
            f"📈 ORB High  : <code>₹{orb_high:,.2f}</code>\n"
            f"📉 ORB Low   : <code>₹{orb_low:,.2f}</code>\n"
            f"📏 Range     : <code>{range_pct:.2f}%</code>\n"
            f"🧭 Direction : <b>{arrow}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━"
        )
        self.send(msg)

    def trade_executed(
        self,
        symbol: str,
        side: str,
        qty: int,
        entry: float,
        sl: float,
        target: float,
    ) -> None:
        """
        Notify that a trade has been placed.

        Args:
            symbol: NSE trading symbol.
            side:   BUY or SELL.
            qty:    Quantity of shares.
            entry:  Entry price.
            sl:     Stop-loss price.
            target: Target price (2-RR).
        """
        emoji = "🟢" if side.upper() == "BUY" else "🔴"
        risk_per_share = abs(entry - sl)
        total_risk = risk_per_share * qty
        msg = (
            f"{emoji} <b>Trade Executed – {side.upper()}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🏷️ Symbol   : <b>{symbol}</b>\n"
            f"📦 Qty      : <code>{qty}</code>\n"
            f"💵 Entry    : <code>₹{entry:,.2f}</code>\n"
            f"🛑 SL       : <code>₹{sl:,.2f}</code>\n"
            f"🎯 Target   : <code>₹{target:,.2f}</code>\n"
            f"⚠️ Risk/sh  : <code>₹{risk_per_share:,.2f}</code>\n"
            f"⚠️ Total    : <code>₹{total_risk:,.2f}</code>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━"
        )
        self.send(msg)

    def partial_profit(
        self,
        symbol: str,
        booked_qty: int,
        booked_price: float,
        new_sl: float,
    ) -> None:
        """
        Notify that partial profits have been booked at 1-RR.

        Args:
            symbol:       NSE trading symbol.
            booked_qty:   Number of shares booked.
            booked_price: Price at which partial was booked.
            new_sl:       Updated stop-loss (moved to entry / cost).
        """
        msg = (
            f"💰 <b>Partial Profit Booked</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🏷️ Symbol     : <b>{symbol}</b>\n"
            f"📦 Booked Qty : <code>{booked_qty}</code>\n"
            f"💵 Price      : <code>₹{booked_price:,.2f}</code>\n"
            f"🛑 New SL     : <code>₹{new_sl:,.2f}</code> (moved to cost)\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━"
        )
        self.send(msg)

    def trade_closed(
        self,
        symbol: str,
        exit_price: float,
        pnl: float,
        reason: str,
    ) -> None:
        """
        Notify that a trade has been fully closed.

        Args:
            symbol:     NSE trading symbol.
            exit_price: The exit / fill price.
            pnl:        Realised profit or loss (₹).
            reason:     Closure reason (TARGET_HIT, STOPPED_OUT,
                        SQUARE_OFF, MANUAL).
        """
        emoji = "✅" if pnl >= 0 else "❌"
        pnl_str = f"+₹{pnl:,.2f}" if pnl >= 0 else f"-₹{abs(pnl):,.2f}"
        msg = (
            f"{emoji} <b>Trade Closed</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🏷️ Symbol : <b>{symbol}</b>\n"
            f"💵 Exit   : <code>₹{exit_price:,.2f}</code>\n"
            f"💰 PnL    : <b>{pnl_str}</b>\n"
            f"📝 Reason : <code>{reason}</code>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━"
        )
        self.send(msg)

    def daily_summary(
        self,
        total_pnl: float,
        trades_count: int,
        wins: int,
        losses: int,
    ) -> None:
        """
        Send end-of-day summary.

        Args:
            total_pnl:    Aggregate PnL for the day (₹).
            trades_count: Total number of trades taken.
            wins:         Number of profitable trades.
            losses:       Number of losing trades.
        """
        emoji = "🟢" if total_pnl >= 0 else "🔴"
        pnl_str = f"+₹{total_pnl:,.2f}" if total_pnl >= 0 else f"-₹{abs(total_pnl):,.2f}"
        win_rate = (wins / trades_count * 100) if trades_count > 0 else 0
        now = datetime.now(IST)
        msg = (
            f"📊 <b>Daily Summary – {now.strftime('%d %b %Y')}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"{emoji} PnL        : <b>{pnl_str}</b>\n"
            f"📈 Trades     : <code>{trades_count}</code>\n"
            f"✅ Wins       : <code>{wins}</code>\n"
            f"❌ Losses     : <code>{losses}</code>\n"
            f"🎯 Win Rate   : <code>{win_rate:.1f}%</code>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━"
        )
        self.send(msg)

    def error_alert(self, error_msg: str) -> None:
        """
        Send an error / warning notification.

        Args:
            error_msg: Description of the error that occurred.
        """
        now = datetime.now(IST)
        msg = (
            f"⚠️ <b>System Alert</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🕘 Time  : <code>{now.strftime('%H:%M:%S')} IST</code>\n"
            f"❗ Error : <code>{self._escape_html(error_msg[:500])}</code>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━"
        )
        self.send(msg)

    def token_expired(self) -> None:
        """
        Urgent notification that the Dhan access token has expired and
        needs to be refreshed via the dashboard.
        """
        now = datetime.now(IST)
        msg = (
            f"🔑 <b>ACCESS TOKEN EXPIRED</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🕘 Time : <code>{now.strftime('%H:%M:%S')} IST</code>\n\n"
            f"⚡ <b>Action Required:</b>\n"
            f"1. Log in to <a href='https://dhanhq.co/'>Dhan</a>\n"
            f"2. Generate a new access token\n"
            f"3. Paste it in the Dashboard → Settings tab\n\n"
            f"⏳ Trading is <b>paused</b> until a valid token is provided.\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━"
        )
        self.send(msg)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _escape_html(text: str) -> str:
        """Escape HTML special characters for Telegram HTML parse mode."""
        return (
            text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )
