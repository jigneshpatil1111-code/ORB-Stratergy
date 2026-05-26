"""
test_strategy_flow.py - Comprehensive test for ORBStrategyEngine
================================================================
Tests the complete lifecycle with MAX_QTY_PER_TRADE=1 (real trading mode).
Covers: ORB candle → pullback → breakout → trade entry (qty=1) →
        1RR target hit → full exit (no partial possible with 1 share).

Also tests: SL hit path, candle DB logging, and trade DB logging.
"""

import sys
import os
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo
from unittest.mock import MagicMock, patch, call

# Configure path so we can import modules from current directory
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

# Force stdout to UTF-8 to prevent UnicodeEncodeError on Windows terminals
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

from config import settings
from strategy import (
    ORBStrategyEngine,
    StockState,
    WAITING_FIRST_CANDLE,
    ANALYZING_ORB,
    WAITING_PULLBACK,
    WAITING_BREAKOUT,
    TRADE_ENTERED,
    PARTIAL_BOOKED,
    STOPPED_OUT,
    SQUARED_OFF,
    REJECTED,
    EXPIRED,
)
from risk_manager import RiskManager

IST = ZoneInfo("Asia/Kolkata")


# ═══════════════════════════════════════════════════════════════════════════
# TEST 1: Full BUY flow with qty=1 → 1RR target → SQUARED_OFF
# ═══════════════════════════════════════════════════════════════════════════

def test_buy_flow_qty1():
    """Test complete BUY lifecycle with 1-share limit."""
    print("\n" + "=" * 60)
    print("TEST 1: BUY flow with qty=1 → 1RR Target → Full Exit")
    print("=" * 60)

    # --- Setup ---
    mock_db = MagicMock()
    mock_db.has_traded_today.return_value = False
    mock_db.log_trade.return_value = 1  # Return trade ID

    mock_broker = MagicMock()
    mock_broker.place_market_order.return_value = {
        "status": "success",
        "data": {"orderId": "LIVE-BUY-001", "orderStatus": "TRADED"}
    }

    mock_notifier = MagicMock()

    # Force MAX_QTY_PER_TRADE=1
    settings.MAX_QTY_PER_TRADE = 1
    settings.PAPER_TRADING = False
    settings.BASE_CAPITAL = 50000
    settings.LEVERAGE = 5
    settings.MAX_RANGE_PCT = 1.5
    settings.MIN_STOCK_PRICE = 60
    settings.MAX_STOCK_PRICE = 5000

    risk_mgr = RiskManager(base_capital=50000, leverage=5)

    engine = ORBStrategyEngine(
        broker=mock_broker,
        risk_mgr=risk_mgr,
        db=mock_db,
        notifier=mock_notifier,
        settings_obj=settings
    )

    universe = [{"security_id": 2885, "symbol": "RELIANCE", "exchange": "NSE_EQ"}]
    engine.initialize_day(universe)

    stock = engine.stocks[2885]
    assert stock.state == WAITING_FIRST_CANDLE
    print(f"✅ Initial State: {stock.state}")

    simulated_time = datetime(2026, 5, 27, 9, 15, 0, tzinfo=IST)

    with patch('strategy._get_ist_now', side_effect=lambda: simulated_time):

        # --- PHASE 1: Build ORB candle (09:15 - 09:19) ---
        print("\n--- Phase 1: Building ORB Candle ---")
        ticks = [
            (datetime(2026, 5, 27, 9, 15, 10, tzinfo=IST), 2500.0),
            (datetime(2026, 5, 27, 9, 16, 0, tzinfo=IST), 2510.0),
            (datetime(2026, 5, 27, 9, 17, 0, tzinfo=IST), 2490.0),
            (datetime(2026, 5, 27, 9, 18, 0, tzinfo=IST), 2515.0),
        ]
        for dt, price in ticks:
            simulated_time = dt
            engine.on_tick({"security_id": 2885, "LTP": price})

        # Finalize at 09:20
        simulated_time = datetime(2026, 5, 27, 9, 20, 0, tzinfo=IST)
        engine.on_tick({"security_id": 2885, "LTP": 2515.0})

        assert stock.state == WAITING_PULLBACK
        assert stock.side == "BUY"
        assert stock.orb_open == 2500.0
        assert stock.orb_high == 2515.0
        assert stock.orb_low == 2490.0
        assert stock.orb_close == 2515.0
        print(f"✅ ORB Candle: O={stock.orb_open} H={stock.orb_high} L={stock.orb_low} C={stock.orb_close}")
        print(f"✅ Direction: {stock.side} (Green candle → BUY)")

        # Verify candle was logged to DB with correct fields
        candle_call = mock_db.log_candle.call_args
        assert candle_call is not None, "log_candle was not called!"
        candle_dict = candle_call[0][0]
        assert "date" in candle_dict, "BUG: 'date' missing from candle log"
        assert "timeframe" in candle_dict, "BUG: 'timeframe' missing from candle log"
        assert "volume" in candle_dict, "BUG: 'volume' missing from candle log"
        assert candle_dict["timeframe"] == "ORB"
        print("✅ Candle DB logging: date, timeframe, volume all present")

        # --- PHASE 2: Pullback (Red candle 9:20-9:25) ---
        print("\n--- Phase 2: Simulating Pullback ---")
        pullback_ticks = [
            (datetime(2026, 5, 27, 9, 20, 10, tzinfo=IST), 2515.0),
            (datetime(2026, 5, 27, 9, 21, 0, tzinfo=IST), 2518.0),
            (datetime(2026, 5, 27, 9, 22, 0, tzinfo=IST), 2512.0),
            (datetime(2026, 5, 27, 9, 24, 59, tzinfo=IST), 2505.0),
        ]
        for dt, price in pullback_ticks:
            simulated_time = dt
            engine.on_tick({"security_id": 2885, "LTP": price})

        # Advance to 9:25 to finalize the pullback candle
        simulated_time = datetime(2026, 5, 27, 9, 25, 0, tzinfo=IST)
        engine.on_tick({"security_id": 2885, "LTP": 2505.0})

        assert stock.state == WAITING_BREAKOUT
        assert stock.pullback_detected == True
        assert stock.pullback_high == 2518.0
        assert stock.pullback_low == 2505.0
        print(f"✅ Pullback: H={stock.pullback_high} L={stock.pullback_low}")
        print(f"✅ State: {stock.state}")

        # --- PHASE 3: Breakout ---
        print("\n--- Phase 3: Simulating Breakout ---")

        # Below ORB high - no trigger
        simulated_time = datetime(2026, 5, 27, 9, 26, 0, tzinfo=IST)
        engine.on_tick({"security_id": 2885, "LTP": 2514.0})
        assert stock.state == WAITING_BREAKOUT

        # Above ORB high - BREAKOUT!
        engine.on_tick({"security_id": 2885, "LTP": 2516.0})

        assert stock.state == TRADE_ENTERED
        assert stock.entry_price == 2516.0
        assert stock.sl_price == 2505.0  # pullback_low
        assert stock.quantity == 1, f"BUG: qty should be 1, got {stock.quantity}"
        assert stock.remaining_qty == 1

        # Target 1RR = 2516 + (2516 - 2505) = 2527
        # Target 2RR = 2516 + 2*(2516 - 2505) = 2538
        assert stock.target_1rr == 2527.0
        assert stock.target_2rr == 2538.0
        print(f"✅ Entry: {stock.entry_price} | SL: {stock.sl_price}")
        print(f"✅ T1(1RR): {stock.target_1rr} | T2(2RR): {stock.target_2rr}")
        print(f"✅ Qty: {stock.quantity} (MAX_QTY_PER_TRADE capped)")

        # Verify broker was called for the entry order
        entry_call = mock_broker.place_market_order.call_args_list[-1]
        assert entry_call[1]["qty"] == 1, "BUG: Broker order qty should be 1"
        assert entry_call[1]["side"] == "BUY"
        print("✅ Broker order: qty=1, side=BUY confirmed")

        # Verify trade was logged to DB with correct fields
        trade_call = mock_db.log_trade.call_args
        trade_dict = trade_call[0][0]
        assert "date" in trade_dict, "BUG: 'date' missing from trade log!"
        assert "target_price" in trade_dict, "BUG: 'target_price' missing (was it 'target_1rr'?)"
        assert "target_1rr" not in trade_dict, "BUG: 'target_1rr' should not be in trade log"
        assert trade_dict["quantity"] == 1
        print("✅ Trade DB logging: date, target_price, quantity=1 all correct")

        # --- PHASE 4: 1RR Target Hit → FULL EXIT (qty=1, no partial) ---
        print("\n--- Phase 4: 1RR Target Hit (Single Share → Full Exit) ---")

        # Just below target - no trigger
        engine.on_tick({"security_id": 2885, "LTP": 2525.0})
        assert stock.state == TRADE_ENTERED

        # Hit 1RR target
        engine.on_tick({"security_id": 2885, "LTP": 2528.0})

        # With qty=1, should go DIRECTLY to SQUARED_OFF (not PARTIAL_BOOKED)
        assert stock.state == SQUARED_OFF, f"BUG: Expected SQUARED_OFF, got {stock.state}"
        assert stock.remaining_qty == 0
        assert stock.trade_pnl > 0
        print(f"✅ State: {stock.state} (direct exit, no partial booking)")
        print(f"✅ P&L: ₹{stock.trade_pnl:.2f}")
        print(f"✅ Remaining: {stock.remaining_qty}")

        # Verify the exit order was placed (SELL 1 share)
        exit_call = mock_broker.place_market_order.call_args_list[-1]
        assert exit_call[1]["qty"] == 1
        assert exit_call[1]["side"] == "SELL"
        print("✅ Exit order: qty=1, side=SELL confirmed")

    print("\n✅ TEST 1 PASSED: BUY flow with qty=1 works correctly!")


# ═══════════════════════════════════════════════════════════════════════════
# TEST 2: SL hit path with qty=1
# ═══════════════════════════════════════════════════════════════════════════

def test_sl_hit_qty1():
    """Test SL hit scenario with 1-share limit."""
    print("\n" + "=" * 60)
    print("TEST 2: BUY flow with qty=1 → SL Hit → STOPPED_OUT")
    print("=" * 60)

    mock_db = MagicMock()
    mock_db.has_traded_today.return_value = False
    mock_db.log_trade.return_value = 2

    mock_broker = MagicMock()
    mock_broker.place_market_order.return_value = {
        "status": "success",
        "data": {"orderId": "LIVE-SL-001", "orderStatus": "TRADED"}
    }

    mock_notifier = MagicMock()
    settings.MAX_QTY_PER_TRADE = 1
    settings.MAX_STOCK_PRICE = 5000

    risk_mgr = RiskManager(base_capital=50000, leverage=5)
    engine = ORBStrategyEngine(
        broker=mock_broker, risk_mgr=risk_mgr,
        db=mock_db, notifier=mock_notifier, settings_obj=settings
    )

    universe = [{"security_id": 1333, "symbol": "HDFC", "exchange": "NSE_EQ"}]
    engine.initialize_day(universe)
    stock = engine.stocks[1333]

    simulated_time = datetime(2026, 5, 27, 9, 15, 0, tzinfo=IST)

    with patch('strategy._get_ist_now', side_effect=lambda: simulated_time):
        # Build ORB candle (Green: open < close)
        for dt, price in [
            (datetime(2026, 5, 27, 9, 15, 10, tzinfo=IST), 1500.0),
            (datetime(2026, 5, 27, 9, 17, 0, tzinfo=IST), 1515.0),
            (datetime(2026, 5, 27, 9, 18, 0, tzinfo=IST), 1498.0),
            (datetime(2026, 5, 27, 9, 19, 0, tzinfo=IST), 1510.0),
        ]:
            simulated_time = dt
            engine.on_tick({"security_id": 1333, "LTP": price})

        # Finalize ORB at 09:20
        simulated_time = datetime(2026, 5, 27, 9, 20, 0, tzinfo=IST)
        engine.on_tick({"security_id": 1333, "LTP": 1510.0})
        assert stock.state == WAITING_PULLBACK, f"Expected WAITING_PULLBACK, got {stock.state}"
        assert stock.side == "BUY", f"Expected BUY, got {stock.side}"
        print(f"✅ ORB finalized: side={stock.side}")

        # Red pullback candle (9:20-9:25)
        for dt, price in [
            (datetime(2026, 5, 27, 9, 20, 5, tzinfo=IST), 1510.0),
            (datetime(2026, 5, 27, 9, 22, 0, tzinfo=IST), 1515.0),
            (datetime(2026, 5, 27, 9, 24, 0, tzinfo=IST), 1500.0),
        ]:
            simulated_time = dt
            engine.on_tick({"security_id": 1333, "LTP": price})

        # Advance to finalize pullback
        simulated_time = datetime(2026, 5, 27, 9, 25, 0, tzinfo=IST)
        engine.on_tick({"security_id": 1333, "LTP": 1502.0})
        assert stock.state == WAITING_BREAKOUT, f"Expected WAITING_BREAKOUT, got {stock.state}"
        print(f"✅ Pullback detected: PB_L={stock.pullback_low}")

        # Breakout above ORB high (1520)
        simulated_time = datetime(2026, 5, 27, 9, 26, 0, tzinfo=IST)
        engine.on_tick({"security_id": 1333, "LTP": 1521.0})
        assert stock.state == TRADE_ENTERED, f"Expected TRADE_ENTERED, got {stock.state}"
        assert stock.quantity == 1, f"Expected qty=1, got {stock.quantity}"
        print(f"✅ Trade entered: entry={stock.entry_price}, sl={stock.sl_price}, qty={stock.quantity}")

        # SL HIT: price drops below SL (pullback_low)
        engine.on_tick({"security_id": 1333, "LTP": stock.sl_price - 1})
        assert stock.state == STOPPED_OUT, f"BUG: Expected STOPPED_OUT, got {stock.state}"
        assert stock.remaining_qty == 0, f"BUG: Expected 0 remaining qty, got {stock.remaining_qty}"
        assert stock.trade_pnl < 0, f"BUG: Expected negative PnL, got {stock.trade_pnl}"
        print(f"✅ SL Hit! State: {stock.state}")
        print(f"✅ Loss P&L: ₹{stock.trade_pnl:.2f}")

    print("\n✅ TEST 2 PASSED: SL hit with qty=1 works correctly!")


# ═══════════════════════════════════════════════════════════════════════════
# TEST 3: Verify RiskManager qty capping
# ═══════════════════════════════════════════════════════════════════════════

def test_risk_manager_qty_cap():
    """Verify that calculate_quantity respects MAX_QTY_PER_TRADE."""
    print("\n" + "=" * 60)
    print("TEST 3: RiskManager qty capping")
    print("=" * 60)

    settings.MAX_QTY_PER_TRADE = 1
    risk_mgr = RiskManager(base_capital=50000, leverage=5)

    # Effective capital = 250000
    # At price 100: normally qty = 2500, should be capped to 1
    qty = risk_mgr.calculate_quantity(100.0)
    assert qty == 1, f"BUG: qty should be 1 (capped), got {qty}"
    print(f"✅ Price ₹100: qty={qty} (capped from 2500)")

    # At price 2500: normally qty = 100, should be capped to 1
    qty = risk_mgr.calculate_quantity(2500.0)
    assert qty == 1, f"BUG: qty should be 1 (capped), got {qty}"
    print(f"✅ Price ₹2500: qty={qty} (capped from 100)")

    # At price 0: should return 0 (invalid)
    qty = risk_mgr.calculate_quantity(0.0)
    assert qty == 0
    print(f"✅ Price ₹0: qty={qty} (invalid price)")

    # At price 300000: normally qty = 0 (too expensive), stays 0
    qty = risk_mgr.calculate_quantity(300000.0)
    assert qty == 0
    print(f"✅ Price ₹300000: qty={qty} (too expensive)")

    print("\n✅ TEST 3 PASSED: Qty capping works correctly!")


# ═══════════════════════════════════════════════════════════════════════════
# TEST 4: Settings validation
# ═══════════════════════════════════════════════════════════════════════════

def test_settings_config():
    """Verify config settings are correct for live trading."""
    print("\n" + "=" * 60)
    print("TEST 4: Settings configuration validation")
    print("=" * 60)

    assert settings.PAPER_TRADING == False, f"BUG: PAPER_TRADING should be False, got {settings.PAPER_TRADING}"
    print(f"✅ PAPER_TRADING = {settings.PAPER_TRADING}")

    assert settings.MAX_QTY_PER_TRADE == 1, f"BUG: MAX_QTY_PER_TRADE should be 1, got {settings.MAX_QTY_PER_TRADE}"
    print(f"✅ MAX_QTY_PER_TRADE = {settings.MAX_QTY_PER_TRADE}")

    assert settings.mode_label == "💰 LIVE"
    print(f"✅ Mode label: {settings.mode_label}")

    print("\n✅ TEST 4 PASSED: Configuration is correct for live trading!")


# ═══════════════════════════════════════════════════════════════════════════
# TEST 5: Verify MIN_STOCK_PRICE and MAX_STOCK_PRICE rejection
# ═══════════════════════════════════════════════════════════════════════════

def test_stock_price_filters():
    """Verify that stocks are rejected if they fall outside MIN_STOCK_PRICE or MAX_STOCK_PRICE."""
    print("\n" + "=" * 60)
    print("TEST 5: Stock price filter validation")
    print("=" * 60)

    # Setup settings
    settings.MIN_STOCK_PRICE = 60
    settings.MAX_STOCK_PRICE = 1000

    # Mock DB
    db_mock = MagicMock()

    # Create strategy engine
    risk_mgr = RiskManager(base_capital=50000, leverage=5)
    engine = ORBStrategyEngine(
        broker=MagicMock(),
        risk_mgr=risk_mgr,
        db=db_mock,
        notifier=MagicMock(),
        settings_obj=settings
    )

    # Mock stock list
    stock_mock_low = MagicMock()
    stock_mock_low.symbol = "PENNY"
    stock_mock_low.orb_close = 50.0
    stock_mock_low.orb_high = 51.0
    stock_mock_low.orb_low = 49.0
    stock_mock_low.state = WAITING_FIRST_CANDLE

    stock_mock_high = MagicMock()
    stock_mock_high.symbol = "EXPENSIVE"
    stock_mock_high.orb_close = 1500.0
    stock_mock_high.orb_high = 1510.0
    stock_mock_high.orb_low = 1490.0
    stock_mock_high.state = WAITING_FIRST_CANDLE

    stock_mock_ok = MagicMock()
    stock_mock_ok.symbol = "OK_STOCK"
    stock_mock_ok.orb_close = 500.0
    stock_mock_ok.orb_high = 502.0
    stock_mock_ok.orb_low = 498.0
    stock_mock_ok.state = WAITING_FIRST_CANDLE

    engine.stocks = {
        1001: stock_mock_low,
        1002: stock_mock_high,
        1003: stock_mock_ok,
    }

    # Run candle finalization
    # 1. Low stock should be rejected
    engine._analyze_orb(1001)
    assert stock_mock_low.state == REJECTED, f"Expected REJECTED, got {stock_mock_low.state}"
    print("✅ Stock with price 50.0 (below min 60) was correctly REJECTED")

    # 2. High stock should be rejected
    engine._analyze_orb(1002)
    assert stock_mock_high.state == REJECTED, f"Expected REJECTED, got {stock_mock_high.state}"
    print("✅ Stock with price 1500.0 (above max 1000) was correctly REJECTED")

    # 3. Ok stock should be processed (state should change to ANALYZING_ORB or WAITING_PULLBACK, depending on logic, but not REJECTED)
    engine._analyze_orb(1003)
    assert stock_mock_ok.state != REJECTED, f"Expected not REJECTED, got {stock_mock_ok.state}"
    print("✅ Stock with price 500.0 (between 60 and 1000) was NOT rejected")

    print("\n✅ TEST 5 PASSED: Stock price filters work correctly!")


# ═══════════════════════════════════════════════════════════════════════════
# Main runner
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("╔══════════════════════════════════════════════════════════╗")
    print("║  ORB Strategy Engine - Comprehensive Test Suite         ║")
    print("║  Testing: Live Trading Mode with qty=1                  ║")
    print("╚══════════════════════════════════════════════════════════╝")

    passed = 0
    failed = 0
    errors = []

    tests = [
        ("Settings Config", test_settings_config),
        ("RiskManager Qty Cap", test_risk_manager_qty_cap),
        ("BUY Flow (qty=1 → 1RR Target)", test_buy_flow_qty1),
        ("SL Hit (qty=1 → STOPPED_OUT)", test_sl_hit_qty1),
        ("Stock Price Filters", test_stock_price_filters),
    ]

    import traceback
    for name, test_func in tests:
        try:
            test_func()
            passed += 1
        except AssertionError as e:
            failed += 1
            errors.append((name, str(e)))
            print(f"\n❌ TEST FAILED: {name}")
            print(f"   Error: {e}")
        except Exception as e:
            failed += 1
            errors.append((name, str(e)))
            print(f"\n❌ TEST ERROR: {name}")
            traceback.print_exc()

    print("\n" + "=" * 60)
    print(f"RESULTS: {passed} passed, {failed} failed")
    if errors:
        print("\nFailed tests:")
        for name, err in errors:
            print(f"  ❌ {name}: {err}")
    else:
        print("\n🎉 ALL TESTS PASSED! System is ready for live trading.")
    print("=" * 60)
