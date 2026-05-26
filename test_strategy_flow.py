"""
test_strategy_flow.py - Unit test script for ORBStrategyEngine
=============================================================
This script mocks the database, broker, and notifier to test the
complete lifecycle of the strategy engine.
"""

import sys
import os
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo
from unittest.mock import MagicMock, patch

# Configure path so we can import modules from current directory
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

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
)
from risk_manager import RiskManager

IST = ZoneInfo("Asia/Kolkata")

def run_test():
    print("Initializing mock components...")
    
    # 1. Mock DB
    mock_db = MagicMock()
    mock_db.has_traded_today.return_value = False
    
    # 2. Mock Broker
    mock_broker = MagicMock()
    # Mock placing order to return a success response with a fake order ID
    mock_broker.place_market_order.return_value = {
        "status": "success",
        "data": {
            "orderId": "TEST-12345",
            "orderStatus": "TRADED"
        }
    }
    
    # 3. Mock Notifier
    mock_notifier = MagicMock()
    
    # 4. Initialize RiskManager (use settings values)
    settings.BASE_CAPITAL = 50000
    settings.LEVERAGE = 5
    settings.MAX_RANGE_PCT = 1.5
    settings.MIN_STOCK_PRICE = 60
    risk_mgr = RiskManager(base_capital=50000, leverage=5)
    
    # Initialize strategy engine
    engine = ORBStrategyEngine(
        broker=mock_broker,
        risk_mgr=risk_mgr,
        db=mock_db,
        notifier=mock_notifier,
        settings_obj=settings
    )
    
    # Define stock universe
    universe = [
        {"security_id": 2885, "symbol": "RELIANCE", "exchange": "NSE_EQ"}
    ]
    engine.initialize_day(universe)
    
    stock = engine.stocks[2885]
    print(f"Initial State: {stock.state}")
    assert stock.state == WAITING_FIRST_CANDLE
    
    # Setup time helper: mock _get_ist_now
    simulated_time = datetime(2026, 5, 27, 9, 15, 0, tzinfo=IST)
    
    def mock_now():
        return simulated_time
        
    # We will patch 'strategy._get_ist_now' and 'utils.get_ist_now'
    with patch('strategy._get_ist_now', side_effect=lambda: simulated_time):
        
        # --- PHASE 1: Build the ORB candle (09:15 - 09:19) ---
        print("\n--- Phase 1: Building ORB Candle ---")
        
        # Ticks for 9:15
        ticks = [
            {"security_id": 2885, "LTP": 2500.0},
            {"security_id": 2885, "LTP": 2510.0},
            {"security_id": 2885, "LTP": 2490.0},
            {"security_id": 2885, "LTP": 2515.0},
        ]
        
        for idx, t in enumerate(ticks):
            simulated_time = datetime(2026, 5, 27, 9, 15 + idx, 0, tzinfo=IST)
            engine.on_tick(t)
            print(f"Tick price {t['LTP']} at {simulated_time.strftime('%H:%M')} | State: {stock.state}")
            
        # Finalize the ORB candle at 09:20
        simulated_time = datetime(2026, 5, 27, 9, 20, 0, tzinfo=IST)
        engine.on_tick({"security_id": 2885, "LTP": 2515.0})
        
        print(f"Finalized ORB Candle O={stock.orb_open}, H={stock.orb_high}, L={stock.orb_low}, C={stock.orb_close}")
        print(f"Range %: {stock.orb_range_pct:.2f}% | Side: {stock.side}")
        print(f"State after ORB finalize: {stock.state}")
        
        # Verify it passed validation and is now WAITING_PULLBACK with BUY direction (Green candle)
        assert stock.state == WAITING_PULLBACK
        assert stock.side == "BUY"
        
        # --- PHASE 2: Wait for Pullback (opposite color candle) ---
        print("\n--- Phase 2: Simulating Pullback (Red Candle 9:20 - 9:25) ---")
        
        # Pullback candle ticks (Open=2515, Close=2505 - Red candle)
        pullback_ticks = [
            # Ticks within 9:20 - 9:25
            (datetime(2026, 5, 27, 9, 20, 10), 2515.0),
            (datetime(2026, 5, 27, 9, 21, 0), 2518.0),
            (datetime(2026, 5, 27, 9, 22, 0), 2512.0),
            (datetime(2026, 5, 27, 9, 24, 59), 2505.0),
        ]
        
        for dt, price in pullback_ticks:
            simulated_time = dt.replace(tzinfo=IST)
            engine.on_tick({"security_id": 2885, "LTP": price})
            
        print(f"Ticks inside pullback candle slot finished. Current state: {stock.state}")
        
        # Advance slot to 9:25 to finalize the 9:20-9:25 candle
        simulated_time = datetime(2026, 5, 27, 9, 25, 0, tzinfo=IST)
        engine.on_tick({"security_id": 2885, "LTP": 2505.0})
        
        print(f"State after pullback candle finalized: {stock.state}")
        print(f"Pullback Detected: {stock.pullback_detected} | PB_H={stock.pullback_high}, PB_L={stock.pullback_low}")
        
        # Verify state transitioned to WAITING_BREAKOUT
        assert stock.state == WAITING_BREAKOUT
        assert stock.pullback_low == 2505.0
        assert stock.pullback_high == 2518.0
        
        # --- PHASE 3: Wait for Breakout (crossing ORB high 2515) ---
        print("\n--- Phase 3: Simulating Breakout ---")
        
        # Let's verify that a tick below breakout high doesn't trigger it
        simulated_time = datetime(2026, 5, 27, 9, 26, 0, tzinfo=IST)
        engine.on_tick({"security_id": 2885, "LTP": 2514.0})
        assert stock.state == WAITING_BREAKOUT
        
        # Send breakout tick at 2516.0 (which is above ORB high 2515.0)
        engine.on_tick({"security_id": 2885, "LTP": 2516.0})
        
        print(f"State after breakout tick: {stock.state}")
        print(f"Entry Price: {stock.entry_price} | SL: {stock.sl_price} | T1 (1RR): {stock.target_1rr} | T2 (2RR): {stock.target_2rr}")
        print(f"Quantity: {stock.quantity}")
        
        # Verify trade entered
        assert stock.state == TRADE_ENTERED
        assert stock.entry_price == 2516.0
        # SL is pullback_low (2505.0)
        assert stock.sl_price == 2505.0
        
        # Target 1RR = entry + risk = 2516 + (2516 - 2505) = 2516 + 11 = 2527
        # Target 2RR = entry + 2*risk = 2516 + 22 = 2538
        assert stock.target_1rr == 2527.0
        assert stock.target_2rr == 2538.0
        
        # --- PHASE 4: Monitor Trade for 1RR Target ---
        print("\n--- Phase 4: Simulating Target 1RR Hit (Partial profit booking) ---")
        
        # Price moves towards target but doesn't hit yet
        engine.on_tick({"security_id": 2885, "LTP": 2525.0})
        assert stock.state == TRADE_ENTERED
        
        # Target hit tick (2528.0 >= target_1rr 2527.0)
        engine.on_tick({"security_id": 2885, "LTP": 2528.0})
        
        print(f"State after Target 1RR hit: {stock.state}")
        print(f"Remaining Qty: {stock.remaining_qty} (Original Qty: {stock.quantity})")
        print(f"New SL Price (Breakeven): {stock.sl_price}")
        print(f"PnL Booked so far: Rs. {stock.trade_pnl:.2f}")
        
        # Verify state is PARTIAL_BOOKED, SL moved to entry (2516.0)
        assert stock.state == PARTIAL_BOOKED
        assert stock.sl_price == 2516.0
        
        # --- PHASE 5: Monitor Trade for 2RR Target ---
        print("\n--- Phase 5: Simulating Target 2RR Hit (Full profit square-off) ---")
        
        # Price goes up to target 2RR (2539.0 >= target_2rr 2538.0)
        engine.on_tick({"security_id": 2885, "LTP": 2539.0})
        
        print(f"State after Target 2RR hit: {stock.state}")
        print(f"Final Trade PnL: Rs. {stock.trade_pnl:.2f}")
        
        # Verify state is SQUARED_OFF
        assert stock.state == SQUARED_OFF
        assert stock.remaining_qty == 0
        
    print("\n[SUCCESS] ALL TESTS PASSED SUCCESSFULLY!")

if __name__ == "__main__":
    run_test()
