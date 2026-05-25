"""
main.py — Master orchestrator for the ORB Intraday Trading System.

Responsibilities:
  1. Bootstrap all subsystems (DB, Broker, Feed, Strategy, Notifier)
  2. Run FastAPI webhook server in a daemon thread
  3. Launch Streamlit dashboard as a subprocess
  4. Schedule market-hours jobs via APScheduler
  5. Manage daily lifecycle: pre-market → trading → square-off → cleanup
  6. Graceful shutdown on SIGINT / SIGTERM

All times in IST (Asia/Kolkata). Currency in INR (₹).
"""

import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, date, time as dtime, timezone, timedelta
from pathlib import Path
from typing import Optional

import uvicorn
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

# ---------------------------------------------------------------------------
# Project imports
# ---------------------------------------------------------------------------
from config import settings
from utils import setup_logging, get_ist_now, is_market_day, is_in_time_window
from database import TradeDB
from broker import DhanBroker
from market_feed import LiveMarketFeed
from strategy import ORBStrategyEngine
from risk_manager import RiskManager
from notifier import TelegramNotifier
from webhook import app as webhook_app, set_strategy_engine, set_broker, set_database

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
IST = timezone(timedelta(hours=5, minutes=30))
PROJECT_ROOT = Path(__file__).resolve().parent
UNIVERSE_PATH = PROJECT_ROOT / "nifty50.json"

logger = logging.getLogger("orb.main")

# ---------------------------------------------------------------------------
# Global references for shutdown handler
# ---------------------------------------------------------------------------
_db: Optional[TradeDB] = None
_broker: Optional[DhanBroker] = None
_feed: Optional[LiveMarketFeed] = None
_strategy: Optional[ORBStrategyEngine] = None
_notifier: Optional[TelegramNotifier] = None
_scheduler: Optional[BackgroundScheduler] = None
_dashboard_proc: Optional[subprocess.Popen] = None
_shutdown_event = threading.Event()


# ╔═══════════════════════════════════════════════════════════════════════════╗
# ║  UNIVERSE LOADER                                                        ║
# ╚═══════════════════════════════════════════════════════════════════════════╝

def load_universe() -> list[dict]:
    """Load the NIFTY 50 stock universe from nifty50.json."""
    if not UNIVERSE_PATH.exists():
        logger.error("Universe file not found: %s", UNIVERSE_PATH)
        raise FileNotFoundError(f"Missing {UNIVERSE_PATH}")

    with open(UNIVERSE_PATH, "r", encoding="utf-8") as fh:
        universe = json.load(fh)

    logger.info("Loaded %d stocks from universe file.", len(universe))
    return universe


# ╔═══════════════════════════════════════════════════════════════════════════╗
# ║  TOKEN MANAGEMENT                                                       ║
# ╚═══════════════════════════════════════════════════════════════════════════╝

def check_and_refresh_token() -> None:
    """
    Check token health. If expired, look in the database for an updated token
    (possibly pasted via the dashboard). If found, hot-swap it into the broker
    and market feed.
    """
    global _broker, _feed, _notifier, _db

    if _broker is None or _db is None or _notifier is None:
        return

    # 1. Try current token
    try:
        healthy = _broker.check_token_health()
    except Exception:
        healthy = False

    if healthy:
        logger.info("Broker token is healthy.")
        return

    logger.warning("Broker token health check FAILED — looking for updated token in DB.")
    _notifier.token_expired()

    # 2. Check DB for a token saved by the dashboard
    try:
        db_token = _db.get_active_token()
    except Exception:
        db_token = None

    if db_token and db_token != settings.DHAN_ACCESS_TOKEN:
        logger.info("Found updated token in DB — applying hot-swap.")
        settings.DHAN_ACCESS_TOKEN = db_token
        _broker.update_token(db_token)
        if _feed is not None:
            _feed.update_token(db_token)
        _db.log_system_event("TOKEN_REFRESHED", "Token hot-swapped from DB at startup.")
        _notifier.send("🔑 Access token refreshed from dashboard.")
    else:
        logger.warning(
            "No updated token in DB. System will retry, but trading may be blocked."
        )
        _notifier.error_alert(
            "⚠️ Dhan token expired and no replacement found in DB. "
            "Please update via dashboard → System Status → Update Token."
        )


# ╔═══════════════════════════════════════════════════════════════════════════╗
# ║  WEBHOOK SERVER (daemon thread)                                         ║
# ╚═══════════════════════════════════════════════════════════════════════════╝

def start_webhook_server() -> threading.Thread:
    """Start FastAPI/uvicorn in a daemon thread."""

    def _run() -> None:
        config = uvicorn.Config(
            app=webhook_app,
            host="0.0.0.0",
            port=8000,
            log_level="warning",
            access_log=False,
        )
        server = uvicorn.Server(config)
        server.run()

    thread = threading.Thread(target=_run, name="webhook-server", daemon=True)
    thread.start()
    logger.info("FastAPI webhook server started on :8000 (daemon thread).")
    return thread


# ╔═══════════════════════════════════════════════════════════════════════════╗
# ║  STREAMLIT DASHBOARD (subprocess)                                       ║
# ╚═══════════════════════════════════════════════════════════════════════════╝

def start_dashboard() -> Optional[subprocess.Popen]:
    """Launch the Streamlit dashboard as a detached subprocess."""
    dashboard_path = PROJECT_ROOT / "dashboard.py"
    if not dashboard_path.exists():
        logger.warning("dashboard.py not found — skipping dashboard launch.")
        return None

    port = os.environ.get("PORT", "8501")
    try:
        proc = subprocess.Popen(
            [
                sys.executable, "-m", "streamlit", "run",
                str(dashboard_path),
                "--server.port", port,
                "--server.headless", "true",
                "--server.address", "0.0.0.0",
                "--browser.gatherUsageStats", "false",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=str(PROJECT_ROOT),
        )
        logger.info("Streamlit dashboard started (PID %d) on :%s.", proc.pid, port)
        return proc
    except Exception as exc:
        logger.error("Failed to start Streamlit dashboard: %s", exc)
        return None


# ╔═══════════════════════════════════════════════════════════════════════════╗
# ║  SCHEDULED JOBS                                                          ║
# ╚═══════════════════════════════════════════════════════════════════════════╝

def pre_market_setup() -> None:
    """
    Runs at 09:14 IST on market days.
    - Refresh token
    - Load universe
    - Initialize strategy engine
    - Start market feed
    """
    global _strategy, _feed, _broker, _db, _notifier

    now = get_ist_now()
    logger.info("═══ PRE-MARKET SETUP — %s ═══", now.strftime("%Y-%m-%d %H:%M:%S"))

    if not is_market_day(now):
        logger.info("Not a market day — skipping.")
        return

    if _broker is None or _db is None or _notifier is None:
        logger.error("Core components not initialised — cannot run pre-market setup.")
        return

    # 1. Token check
    check_and_refresh_token()

    # 2. Load universe
    try:
        universe = load_universe()
    except FileNotFoundError:
        _notifier.error_alert("❌ Universe file (nifty50.json) not found!")
        return

    # 3. Initialize strategy
    risk_mgr = RiskManager(base_capital=settings.BASE_CAPITAL, leverage=settings.LEVERAGE)
    _strategy = ORBStrategyEngine(
        broker=_broker,
        risk_mgr=risk_mgr,
        db=_db,
        notifier=_notifier,
        settings=settings,
    )
    _strategy.initialize_day(universe)
    set_strategy_engine(_strategy)

    # 4. Prepare instruments for market feed
    instruments = [
        (stock.get("exchange", "NSE_EQ"), stock["security_id"])
        for stock in universe
    ]

    # 5. Start / restart market feed
    if _feed is not None:
        try:
            _feed.stop()
        except Exception:
            pass

    _feed = LiveMarketFeed(
        client_id=settings.DHAN_CLIENT_ID,
        access_token=settings.DHAN_ACCESS_TOKEN,
    )
    _feed.set_instruments(instruments)
    _feed.set_on_tick(_strategy.on_tick)
    _feed.start()

    _db.log_system_event("PRE_MARKET_SETUP", f"Universe: {len(universe)} stocks. Feed started.")
    _notifier.send(f"📡 Pre-market setup complete. Tracking {len(universe)} stocks. Waiting for 09:20 ORB candle…")
    logger.info("Pre-market setup complete. %d instruments subscribed.", len(instruments))


def force_square_off() -> None:
    """
    Runs at 14:30 IST — force exit all open positions.
    """
    global _strategy, _notifier, _db

    now = get_ist_now()
    logger.info("═══ FORCE SQUARE-OFF — %s ═══", now.strftime("%H:%M:%S"))

    if _strategy is None:
        logger.info("No strategy engine active — nothing to square off.")
        return

    try:
        _strategy.force_square_off_all()
        if _notifier:
            _notifier.send("⏰ 14:30 — All open positions squared off.")
        if _db:
            _db.log_system_event("SQUARE_OFF", "All positions squared off at 14:30.")
    except Exception as exc:
        logger.exception("Error during forced square-off: %s", exc)
        if _notifier:
            _notifier.error_alert(f"Square-off error: {exc}")


def end_of_day_cleanup() -> None:
    """
    Runs at 15:35 IST — stop feed, send daily summary, persist state.
    """
    global _strategy, _feed, _notifier, _db

    now = get_ist_now()
    logger.info("═══ END-OF-DAY CLEANUP — %s ═══", now.strftime("%H:%M:%S"))

    # 1. Stop market feed
    if _feed is not None:
        try:
            _feed.stop()
            logger.info("Market feed stopped.")
        except Exception as exc:
            logger.warning("Error stopping feed: %s", exc)

    # 2. Daily summary via Telegram
    if _db is not None and _notifier is not None:
        try:
            today_trades = _db.get_today_trades()
            total_pnl = sum(t.get("pnl", 0) for t in today_trades)
            wins = sum(1 for t in today_trades if t.get("pnl", 0) > 0)
            losses = sum(1 for t in today_trades if t.get("pnl", 0) < 0)
            _notifier.daily_summary(
                total_pnl=total_pnl,
                trades_count=len(today_trades),
                wins=wins,
                losses=losses,
            )
        except Exception as exc:
            logger.warning("Failed to send daily summary: %s", exc)

    # 3. Export CSV
    if _db is not None:
        try:
            date_str = now.strftime("%Y-%m-%d")
            csv_path = _db.export_csv(date_str)
            logger.info("Trade CSV exported: %s", csv_path)
            _db.log_system_event("EOD_CLEANUP", f"Cleanup done. CSV: {csv_path}")
        except Exception as exc:
            logger.warning("CSV export failed: %s", exc)

    logger.info("End-of-day cleanup complete.")


# ╔═══════════════════════════════════════════════════════════════════════════╗
# ║  GRACEFUL SHUTDOWN                                                       ║
# ╚═══════════════════════════════════════════════════════════════════════════╝

def graceful_shutdown(signum: Optional[int] = None, frame: object = None) -> None:
    """
    Handle SIGINT / SIGTERM gracefully:
      1. Stop market feed
      2. Square off open positions
      3. Close DB
      4. Kill dashboard subprocess
      5. Send Telegram alert
    """
    global _feed, _strategy, _db, _notifier, _scheduler, _dashboard_proc

    if _shutdown_event.is_set():
        return  # Prevent double-entry
    _shutdown_event.set()

    sig_name = signal.Signals(signum).name if signum else "MANUAL"
    logger.info("═══ GRACEFUL SHUTDOWN triggered (%s) ═══", sig_name)

    # 1. Stop market feed
    if _feed is not None:
        try:
            _feed.stop()
            logger.info("Market feed stopped.")
        except Exception as exc:
            logger.warning("Error stopping feed: %s", exc)

    # 2. Square off open positions
    if _strategy is not None:
        try:
            active = _strategy.get_active_trades()
            if active:
                logger.info("Squaring off %d active trades before exit…", len(active))
                _strategy.force_square_off_all()
        except Exception as exc:
            logger.warning("Error during shutdown square-off: %s", exc)

    # 3. Stop scheduler
    if _scheduler is not None:
        try:
            _scheduler.shutdown(wait=False)
            logger.info("Scheduler stopped.")
        except Exception:
            pass

    # 4. Kill dashboard
    if _dashboard_proc is not None:
        try:
            _dashboard_proc.terminate()
            _dashboard_proc.wait(timeout=5)
            logger.info("Dashboard subprocess terminated.")
        except Exception:
            try:
                _dashboard_proc.kill()
            except Exception:
                pass

    # 5. Send Telegram notification
    if _notifier is not None:
        try:
            _notifier.send("🛑 ORB Trading System stopped.")
        except Exception:
            pass

    # 6. Log & close DB
    if _db is not None:
        try:
            _db.log_system_event("SYSTEM_SHUTDOWN", f"Shutdown signal: {sig_name}")
        except Exception:
            pass

    logger.info("Shutdown complete. Goodbye.")
    sys.exit(0)


# ╔═══════════════════════════════════════════════════════════════════════════╗
# ║  MAIN                                                                    ║
# ╚═══════════════════════════════════════════════════════════════════════════╝

def main() -> None:
    """Entry-point: wire everything together and start the event loop."""
    global _db, _broker, _feed, _strategy, _notifier, _scheduler, _dashboard_proc

    # ── 1. Logging ──────────────────────────────────────────────────────────
    setup_logging()
    logger.info("╔════════════════════════════════════════════════════════╗")
    logger.info("║   ORB INTRADAY TRADING SYSTEM — Starting up…         ║")
    logger.info("╚════════════════════════════════════════════════════════╝")

    # ── 2. Signal handlers ──────────────────────────────────────────────────
    signal.signal(signal.SIGINT, graceful_shutdown)
    signal.signal(signal.SIGTERM, graceful_shutdown)

    # ── 3. Core components ──────────────────────────────────────────────────
    logger.info("Initialising core components…")

    # Database
    os.makedirs(os.path.dirname(settings.DB_PATH) or ".", exist_ok=True)
    _db = TradeDB(settings.DB_PATH)
    _db.log_system_event("SYSTEM_START", f"Startup at {get_ist_now().isoformat()}")

    # Broker
    _broker = DhanBroker(
        client_id=settings.DHAN_CLIENT_ID,
        access_token=settings.DHAN_ACCESS_TOKEN,
    )

    # Notifier
    _notifier = TelegramNotifier(
        bot_token=settings.TELEGRAM_BOT_TOKEN,
        chat_id=settings.TELEGRAM_CHAT_ID,
    )

    # ── 4. Token health ────────────────────────────────────────────────────
    check_and_refresh_token()

    # ── 5. Load universe (validate file exists) ─────────────────────────────
    try:
        universe = load_universe()
        logger.info("Universe validated: %d stocks.", len(universe))
    except FileNotFoundError:
        logger.warning("Universe file missing — will retry at pre-market time.")

    # ── 6. Inject references into webhook module ───────────────────────────
    set_database(_db)
    set_broker(_broker)
    # Strategy engine will be injected during pre_market_setup()

    # ── 7. Telegram start notification ─────────────────────────────────────
    try:
        _notifier.system_start()
    except Exception as exc:
        logger.warning("Failed to send Telegram start notification: %s", exc)

    # ── 8. Start webhook server (daemon thread) ───────────────────────────
    webhook_thread = start_webhook_server()

    # ── 9. Start dashboard subprocess ─────────────────────────────────────
    _dashboard_proc = start_dashboard()

    # ── 10. APScheduler — time-based jobs ─────────────────────────────────
    _scheduler = BackgroundScheduler(timezone="Asia/Kolkata")

    # Job 1: Pre-market setup at 09:14 IST, Mon–Fri
    _scheduler.add_job(
        pre_market_setup,
        CronTrigger(hour=9, minute=14, day_of_week="mon-fri", timezone="Asia/Kolkata"),
        id="pre_market_setup",
        name="Pre-market setup (09:14)",
        misfire_grace_time=120,
        replace_existing=True,
    )

    # Job 2: Force square-off at 14:30 IST, Mon–Fri
    _scheduler.add_job(
        force_square_off,
        CronTrigger(hour=14, minute=30, day_of_week="mon-fri", timezone="Asia/Kolkata"),
        id="force_square_off",
        name="Force square-off (14:30)",
        misfire_grace_time=60,
        replace_existing=True,
    )

    # Job 3: End-of-day cleanup at 15:35 IST, Mon–Fri
    _scheduler.add_job(
        end_of_day_cleanup,
        CronTrigger(hour=15, minute=35, day_of_week="mon-fri", timezone="Asia/Kolkata"),
        id="eod_cleanup",
        name="End-of-day cleanup (15:35)",
        misfire_grace_time=120,
        replace_existing=True,
    )

    _scheduler.start()
    logger.info(
        "APScheduler started with %d jobs: %s",
        len(_scheduler.get_jobs()),
        [j.name for j in _scheduler.get_jobs()],
    )

    # ── 11. If we're within market hours, run pre-market now ──────────────
    now = get_ist_now()
    if is_market_day(now):
        market_open_dt = now.replace(hour=9, minute=14, second=0, microsecond=0)
        market_close_dt = now.replace(hour=15, minute=35, second=0, microsecond=0)
        if market_open_dt <= now <= market_close_dt:
            logger.info("Within market hours — triggering pre_market_setup immediately.")
            pre_market_setup()

    # ── 12. Main idle loop ────────────────────────────────────────────────
    logger.info("System running. Waiting for scheduled events…")
    logger.info("  Webhook: http://0.0.0.0:8000/health")
    logger.info("  Dashboard: http://0.0.0.0:8501")
    logger.info("  Press Ctrl+C to stop.")

    try:
        while not _shutdown_event.is_set():
            _shutdown_event.wait(timeout=30)

            # Periodic heartbeat log (every 30 s)
            if not _shutdown_event.is_set():
                now = get_ist_now()
                feed_status = "connected" if (_feed and _feed.is_connected()) else "disconnected"
                active_count = 0
                if _strategy:
                    try:
                        active_count = len(_strategy.get_active_trades())
                    except Exception:
                        pass
                logger.debug(
                    "Heartbeat | %s | Feed: %s | Active trades: %d",
                    now.strftime("%H:%M:%S"),
                    feed_status,
                    active_count,
                )
    except KeyboardInterrupt:
        graceful_shutdown(signum=signal.SIGINT)


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    main()
