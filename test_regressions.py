import sqlite3
from datetime import date, datetime
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from dhanhq import MarketFeed

import main
import webhook
from config import IST, settings
from database import TradeDB
from market_feed import LiveMarketFeed
from risk_manager import RiskManager
from strategy import ORBStrategyEngine, SQUARED_OFF, TRADE_ENTERED, WAITING_PULLBACK
from utils import is_market_day


def _create_legacy_trades_table(path: str) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """
            CREATE TABLE trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                security_id INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                quantity INTEGER NOT NULL DEFAULT 0,
                entry_price REAL NOT NULL DEFAULT 0.0,
                sl_price REAL NOT NULL DEFAULT 0.0,
                target_price REAL NOT NULL DEFAULT 0.0,
                exit_price REAL DEFAULT NULL,
                pnl REAL DEFAULT NULL,
                status TEXT NOT NULL DEFAULT 'OPEN',
                entry_time TEXT DEFAULT NULL,
                exit_time TEXT DEFAULT NULL,
                order_id TEXT DEFAULT NULL,
                partial_order_id TEXT DEFAULT NULL,
                remaining_qty INTEGER DEFAULT 0
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def test_legacy_database_migrates_trade_lifecycle_fields(tmp_path):
    db_path = str(tmp_path / "legacy.db")
    _create_legacy_trades_table(db_path)

    db = TradeDB(db_path)
    trade_id = db.log_trade({
        "date": datetime.now(IST).strftime("%Y-%m-%d"),
        "security_id": 2885,
        "symbol": "RELIANCE",
        "side": "BUY",
    })
    db.update_trade(trade_id, {
        "status": "SQUARED_OFF",
        "partial_qty": 1,
        "partial_price": 100.5,
        "partial_pnl": 0.5,
        "exit_reason": "Target Hit",
    })

    trade = db.get_today_trades()[0]
    assert trade["partial_qty"] == 1
    assert trade["partial_price"] == 100.5
    assert trade["partial_pnl"] == 0.5
    assert trade["exit_reason"] == "Target Hit"


def test_trade_update_rejects_unknown_columns(tmp_path):
    db = TradeDB(str(tmp_path / "guarded.db"))
    with pytest.raises(ValueError, match="Unsupported trade update columns"):
        db.update_trade(1, {"not_a_trade_column": "bad"})


def test_webhook_stores_a_valid_audit_candle(tmp_path, monkeypatch):
    db = TradeDB(str(tmp_path / "webhook.db"))
    monkeypatch.setattr(settings, "WEBHOOK_SECRET", "test-secret")
    webhook.set_database(db)

    client = TestClient(webhook.app)
    response = client.post(
        "/webhook",
        headers={"X-Webhook-Secret": "test-secret"},
        json={"symbol": "infy", "action": "buy", "price": 1500.25},
    )

    assert response.status_code == 200
    conn = sqlite3.connect(str(tmp_path / "webhook.db"))
    try:
        date_value, timeframe = conn.execute(
            "SELECT date, timeframe FROM candles"
        ).fetchone()
    finally:
        conn.close()
    assert date_value
    assert timeframe == "WEBHOOK_SIGNAL"


@pytest.mark.parametrize("placeholder", ["changeme", "replace_with_a_long_random_secret"])
def test_webhook_is_disabled_for_placeholder_secret(tmp_path, monkeypatch, placeholder):
    db = TradeDB(str(tmp_path / "disabled-webhook.db"))
    monkeypatch.setattr(settings, "WEBHOOK_SECRET", placeholder)
    webhook.set_database(db)

    response = TestClient(webhook.app).post(
        "/webhook",
        headers={"X-Webhook-Secret": placeholder},
        json={"symbol": "INFY", "action": "BUY", "price": 1500.25},
    )

    assert response.status_code == 503


def test_feed_normalises_config_exchange_segments():
    feed = LiveMarketFeed("client", "token")
    feed.set_instruments([("NSE_EQ", 2885)])
    assert feed._instruments == [(MarketFeed.NSE, "2885")]

    with pytest.raises(ValueError, match="Unsupported market-feed exchange"):
        feed.set_instruments([("UNKNOWN", 1)])


def test_transition_is_persisted_for_dashboard(tmp_path, monkeypatch):
    db = TradeDB(str(tmp_path / "state.db"))
    engine = ORBStrategyEngine(
        broker=MagicMock(),
        risk_mgr=RiskManager(base_capital=50000, leverage=5),
        db=db,
        notifier=MagicMock(),
        settings_obj=settings,
    )
    engine.initialize_day([{"security_id": 2885, "symbol": "RELIANCE"}])

    def transition(sec_id, tick, now):
        engine.stocks[sec_id].state = WAITING_PULLBACK

    monkeypatch.setattr(engine, "_handle_first_candle_tick", transition)
    engine.on_tick({"security_id": 2885, "LTP": 100.25})

    saved = db.load_stock_states()
    assert saved[0]["state"] == WAITING_PULLBACK
    assert saved[0]["ltp"] == 100.25


def test_square_off_uses_safe_pnl_estimate_when_ltp_unavailable():
    broker = MagicMock()
    broker.get_ltp.return_value = 0.0
    engine = ORBStrategyEngine(
        broker=broker,
        risk_mgr=RiskManager(base_capital=50000, leverage=5),
        db=MagicMock(),
        notifier=MagicMock(),
        settings_obj=settings,
    )
    engine.initialize_day([{"security_id": 2885, "symbol": "RELIANCE"}])
    stock = engine.stocks[2885]
    stock.state = TRADE_ENTERED
    stock.side = "BUY"
    stock.entry_price = 100.0
    stock.quantity = 1
    stock.remaining_qty = 1

    engine.force_square_off_all()

    assert stock.state == SQUARED_OFF
    assert stock.trade_pnl == 0.0
    broker.place_market_order.assert_called_once()


def test_pre_market_setup_passes_settings_object(monkeypatch):
    constructed = {}

    class DummyStrategy:
        def __init__(self, *, settings_obj, **kwargs):
            constructed["settings_obj"] = settings_obj

        def initialize_day(self, universe):
            constructed["universe"] = universe

        def restore_states(self):
            constructed["restored"] = True

        def on_tick(self, tick):
            return None

    feed = MagicMock()
    monkeypatch.setattr(main, "_broker", MagicMock())
    monkeypatch.setattr(main, "_db", MagicMock())
    monkeypatch.setattr(main, "_notifier", MagicMock())
    monkeypatch.setattr(main, "_feed", None)
    monkeypatch.setattr(main, "is_market_day", lambda now: True)
    monkeypatch.setattr(main, "check_and_refresh_token", lambda: True)
    monkeypatch.setattr(
        main,
        "load_universe",
        lambda: [{"security_id": 2885, "symbol": "RELIANCE", "exchange": "NSE_EQ"}],
    )
    monkeypatch.setattr(main, "ORBStrategyEngine", DummyStrategy)
    monkeypatch.setattr(main, "LiveMarketFeed", lambda **kwargs: feed)
    monkeypatch.setattr(main, "set_strategy_engine", lambda strategy: None)

    main.pre_market_setup()

    assert constructed["settings_obj"] is settings
    assert constructed["universe"][0]["symbol"] == "RELIANCE"
    assert constructed["restored"] is True
    feed.start.assert_called_once_with()


def test_pre_market_setup_aborts_when_token_is_invalid(monkeypatch):
    db = MagicMock()
    feed_factory = MagicMock()
    monkeypatch.setattr(main, "_broker", MagicMock())
    monkeypatch.setattr(main, "_db", db)
    monkeypatch.setattr(main, "_notifier", MagicMock())
    monkeypatch.setattr(main, "_feed", None)
    monkeypatch.setattr(main, "_strategy", None)
    monkeypatch.setattr(main, "is_market_day", lambda now: True)
    monkeypatch.setattr(main, "check_and_refresh_token", lambda: False)
    monkeypatch.setattr(main, "LiveMarketFeed", feed_factory)
    monkeypatch.setattr(
        main,
        "load_universe",
        lambda: pytest.fail("universe should not load with an invalid token"),
    )

    main.pre_market_setup()

    feed_factory.assert_not_called()
    assert main._strategy is None
    db.log_system_event.assert_called_once_with(
        "PRE_MARKET_ABORTED",
        "No valid Dhan access token; live feed and automatic orders were not started.",
    )


def test_official_nse_2026_calendar_allows_may_27_and_blocks_may_28():
    assert is_market_day(date(2026, 5, 27)) is True
    assert is_market_day(date(2026, 5, 28)) is False


@pytest.mark.parametrize(
    "holiday",
    [
        date(2026, 3, 3),
        date(2026, 3, 26),
        date(2026, 3, 31),
        date(2026, 6, 26),
        date(2026, 9, 14),
        date(2026, 10, 20),
        date(2026, 11, 10),
    ],
)
def test_official_nse_2026_weekday_holidays_are_not_traded(holiday):
    assert is_market_day(holiday) is False
