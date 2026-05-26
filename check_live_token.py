"""
check_live_token.py - Verifies if the Dhan API token in .env is valid.
"""

import sys
import os
from unittest.mock import patch

# Configure path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from config import settings
from broker import DhanBroker

def test_token():
    print("Loading settings from .env...")
    # Validate settings
    issues = settings.validate()
    # If DHAN credentials are missing, print issues
    if not settings.DHAN_CLIENT_ID or not settings.DHAN_ACCESS_TOKEN:
        print("[ERROR] DHAN_CLIENT_ID or DHAN_ACCESS_TOKEN is missing in .env!")
        for issue in issues:
            print(f" - {issue}")
        return

    print(f"Client ID: {settings.DHAN_CLIENT_ID}")
    masked_token = settings.DHAN_ACCESS_TOKEN[-6:] if settings.DHAN_ACCESS_TOKEN else "N/A"
    print(f"Access Token ends with: ...{masked_token}")

    print("Connecting to Dhan API to verify token health...")
    
    # Temporarily disable PAPER_TRADING flag just to perform live health check
    original_paper_trading = settings.PAPER_TRADING
    settings.PAPER_TRADING = False
    
    try:
        broker = DhanBroker(
            client_id=settings.DHAN_CLIENT_ID,
            access_token=settings.DHAN_ACCESS_TOKEN
        )
        
        is_healthy = broker.check_token_health()
        if is_healthy:
            print("\n[SUCCESS] Your Dhan API Token is VALID and working perfectly!")
            # Get fund limits as extra proof
            funds = broker.get_fund_limits()
            if funds:
                print(f"Connection active. Successfully retrieved account details.")
        else:
            print("\n[FAILED] Dhan API health check failed. The token is either invalid or expired.")
    except Exception as e:
        print(f"\n[ERROR] An error occurred while connecting: {e}")
    finally:
        settings.PAPER_TRADING = original_paper_trading

if __name__ == "__main__":
    test_token()
