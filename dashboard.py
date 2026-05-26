"""
dashboard.py — Streamlit dashboard with DARK CYBER-TRADING aesthetic.

Features:
  • Password authentication screen
  • Daily Dhan access token update
  • 5 tabs: Live Scanner, Active Positions, Trade History, Export, System Status
  • Auto-refresh every 5 seconds via streamlit-autorefresh
  • TradingView Lightweight Charts widget (NIFTY 50 index)
  • Dark cyber theme with neon glow effects

Run:
    streamlit run dashboard.py --server.port 8501 --server.headless true

All times in IST (Asia/Kolkata). Currency in INR (₹).
"""

import os
import sys
import time
import hashlib
from datetime import datetime, timezone, timedelta, date

import streamlit as st
import pandas as pd

# ---------------------------------------------------------------------------
# Path bootstrap — ensure project root is importable when Streamlit is
# launched from a different CWD.
# ---------------------------------------------------------------------------
_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from config import settings  # noqa: E402
from database import TradeDB  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
IST = timezone(timedelta(hours=5, minutes=30))
_BOOT_TIME = datetime.now(IST)
APP_VERSION = "1.0.0"

# ---------------------------------------------------------------------------
# Streamlit page config — MUST be first Streamlit call
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="ORB Intraday Trader",
    page_icon="🎯",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ---------------------------------------------------------------------------
# Dark cyber CSS — injected once per session
# ---------------------------------------------------------------------------
DARK_CYBER_CSS = """
<style>
:root {
    --bg-primary: #0a0a0f;
    --bg-secondary: #12121a;
    --bg-card: #1a1a2e;
    --accent-green: #00ff88;
    --accent-red: #ff4757;
    --accent-blue: #00d4ff;
    --accent-gold: #ffd700;
    --text-primary: #e0e0e0;
    --text-secondary: #888;
}

/* Global */
.stApp, [data-testid="stAppViewContainer"], [data-testid="stHeader"] {
    background-color: var(--bg-primary) !important;
    color: var(--text-primary) !important;
}
section[data-testid="stSidebar"] {
    background-color: var(--bg-secondary) !important;
}

/* Metric cards */
[data-testid="stMetric"] {
    background: var(--bg-card);
    border: 1px solid rgba(0,212,255,0.15);
    border-radius: 12px;
    padding: 16px;
    box-shadow: 0 0 15px rgba(0,212,255,0.07);
    transition: box-shadow 0.3s ease;
}
[data-testid="stMetric"]:hover {
    box-shadow: 0 0 25px rgba(0,255,136,0.15);
}
[data-testid="stMetricLabel"] {
    color: var(--text-secondary) !important;
    font-size: 0.85rem !important;
    text-transform: uppercase;
    letter-spacing: 1px;
}
[data-testid="stMetricValue"] {
    color: var(--accent-blue) !important;
    font-weight: 700 !important;
}

/* Tabs */
.stTabs [data-baseweb="tab-list"] {
    gap: 8px;
    background: var(--bg-secondary);
    border-radius: 10px;
    padding: 4px;
}
.stTabs [data-baseweb="tab"] {
    color: var(--text-secondary) !important;
    border-radius: 8px;
    font-weight: 600;
}
.stTabs [aria-selected="true"] {
    background: var(--bg-card) !important;
    color: var(--accent-green) !important;
    border-bottom: 2px solid var(--accent-green) !important;
}

/* Tables / dataframes */
[data-testid="stDataFrame"] {
    border: 1px solid rgba(0,212,255,0.1);
    border-radius: 10px;
    overflow: hidden;
}
.stDataFrame thead th {
    background: var(--bg-card) !important;
    color: var(--accent-blue) !important;
    font-weight: 700 !important;
    text-transform: uppercase;
    letter-spacing: 0.5px;
}
.stDataFrame tbody td {
    color: var(--text-primary) !important;
    border-bottom: 1px solid rgba(255,255,255,0.03) !important;
}

/* Buttons */
.stButton > button {
    background: linear-gradient(135deg, #00d4ff 0%, #00ff88 100%) !important;
    color: #0a0a0f !important;
    font-weight: 700 !important;
    border: none !important;
    border-radius: 8px !important;
    padding: 8px 24px !important;
    transition: all 0.3s ease !important;
}
.stButton > button:hover {
    box-shadow: 0 0 20px rgba(0,255,136,0.4) !important;
    transform: translateY(-1px);
}

/* Inputs */
.stTextInput input, .stDateInput input {
    background: var(--bg-card) !important;
    color: var(--text-primary) !important;
    border: 1px solid rgba(0,212,255,0.2) !important;
    border-radius: 8px !important;
}
.stTextInput input:focus, .stDateInput input:focus {
    border-color: var(--accent-green) !important;
    box-shadow: 0 0 10px rgba(0,255,136,0.2) !important;
}

/* Neon title */
.neon-title {
    text-align: center;
    font-size: 2.2rem;
    font-weight: 800;
    background: linear-gradient(90deg, #00d4ff, #00ff88, #ffd700);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    text-shadow: 0 0 30px rgba(0,212,255,0.3);
    margin-bottom: 0.2rem;
}
.neon-subtitle {
    text-align: center;
    color: var(--text-secondary);
    font-size: 0.9rem;
    margin-bottom: 1.5rem;
}

/* Status dots */
.dot-green { display:inline-block; width:12px; height:12px; border-radius:50%; background:#00ff88; box-shadow:0 0 8px #00ff88; margin-right:6px; }
.dot-red   { display:inline-block; width:12px; height:12px; border-radius:50%; background:#ff4757; box-shadow:0 0 8px #ff4757; margin-right:6px; }
.dot-yellow{ display:inline-block; width:12px; height:12px; border-radius:50%; background:#ffd700; box-shadow:0 0 8px #ffd700; margin-right:6px; }

/* Paper trading badge */
.paper-badge {
    display:inline-block;
    padding: 6px 18px;
    background: rgba(255,215,0,0.15);
    border: 2px solid #ffd700;
    border-radius: 20px;
    color: #ffd700;
    font-weight: 700;
    font-size: 0.95rem;
    letter-spacing: 1px;
    animation: pulse-gold 2s infinite;
}
@keyframes pulse-gold {
    0%, 100% { box-shadow: 0 0 5px rgba(255,215,0,0.3); }
    50% { box-shadow: 0 0 20px rgba(255,215,0,0.6); }
}

/* Card wrapper */
.cyber-card {
    background: var(--bg-card);
    border: 1px solid rgba(0,212,255,0.12);
    border-radius: 12px;
    padding: 20px;
    margin-bottom: 16px;
    box-shadow: 0 0 12px rgba(0,0,0,0.4);
}

/* Scrollbar */
::-webkit-scrollbar { width: 6px; height: 6px; }
::-webkit-scrollbar-track { background: var(--bg-secondary); }
::-webkit-scrollbar-thumb { background: var(--accent-blue); border-radius: 3px; }

/* Hide Streamlit branding */
#MainMenu, footer, header { visibility: hidden; }
</style>
"""

st.markdown(DARK_CYBER_CSS, unsafe_allow_html=True)

# ---------------------------------------------------------------------------
# Auto-refresh (5 seconds)
# ---------------------------------------------------------------------------
try:
    from streamlit_autorefresh import st_autorefresh
    st_autorefresh(interval=5000, limit=None, key="orb_autorefresh")
except ImportError:
    # Graceful fallback — user just needs to refresh manually
    pass

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_ist() -> datetime:
    return datetime.now(IST)


def _get_db() -> TradeDB:
    """Lazy-load a TradeDB bound to current settings."""
    return TradeDB(settings.DB_PATH)


def _state_color(state: str) -> str:
    """Map stock state to a display color name."""
    _map = {
        "TRADE_ENTERED": "🟢",
        "PARTIAL_BOOKED": "🟢",
        "WAITING_PULLBACK": "🟡",
        "WAITING_BREAKOUT": "🟡",
        "ANALYZING_ORB": "🟡",
        "WAITING_FIRST_CANDLE": "⚪",
        "STOPPED_OUT": "🔴",
        "REJECTED": "🔴",
        "SQUARED_OFF": "⚪",
        "EXPIRED": "⚪",
    }
    return _map.get(state, "⚪")


def _pnl_html(pnl: float) -> str:
    """Return styled P&L string."""
    color = "#00ff88" if pnl >= 0 else "#ff4757"
    sign = "+" if pnl >= 0 else ""
    return f'<span style="color:{color};font-weight:700;">{sign}₹{pnl:,.2f}</span>'

# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

def _check_password() -> bool:
    """Render login screen and return True when authenticated."""
    if st.session_state.get("authenticated"):
        return True

    st.markdown('<div class="neon-title">🎯 ORB INTRADAY TRADER</div>', unsafe_allow_html=True)
    st.markdown('<div class="neon-subtitle">Opening Range Breakout · NIFTY 50 · Automated</div>', unsafe_allow_html=True)
    st.markdown("---")

    col1, col2, col3 = st.columns([1, 2, 1])
    with col2:
        st.markdown("#### 🔐 Dashboard Login")
        password = st.text_input("Password", type="password", key="login_pw", placeholder="Enter dashboard password")
        if st.button("Login", key="login_btn", use_container_width=True):
            expected = os.environ.get("DASHBOARD_PASSWORD", "") or getattr(settings, "DASHBOARD_PASSWORD", "")
            if password and password == expected:
                st.session_state["authenticated"] = True
                st.rerun()
            else:
                st.error("❌ Invalid password. Please try again.")
    return False


# ---------------------------------------------------------------------------
# Tab renderers
# ---------------------------------------------------------------------------

def _render_live_scanner(db: TradeDB) -> None:
    """Tab 1 — 📡 Live Scanner."""
    st.markdown("### 📡 Live Scanner — NIFTY 50 Universe")

    # --- Search Filter ---
    search_q = st.text_input("🔍 Search Stocks in Live Scanner", placeholder="Type symbol to filter (e.g. INFY)").strip().upper()

    # --- Load stock states from DB ---
    try:
        states = db.load_stock_states()
    except Exception:
        states = []

    if states:
        rows = []
        for s in states:
            sym = s.get("symbol", "").upper()
            if search_q and search_q not in sym:
                continue
            rows.append({
                "Status": _state_color(s.get("state", "")),
                "Symbol": s.get("symbol", ""),
                "State": s.get("state", ""),
                "ORB High": f"₹{s.get('orb_high', 0):.2f}",
                "ORB Low": f"₹{s.get('orb_low', 0):.2f}",
                "Range %": f"{s.get('orb_range_pct', 0):.2f}%",
                "Direction": "🟢 LONG" if s.get("orb_is_green") else "🔴 SHORT",
                "LTP": f"₹{s.get('ltp', 0):.2f}" if s.get("ltp") else "—",
            })
        if rows:
            df = pd.DataFrame(rows)
            st.dataframe(df, use_container_width=True, hide_index=True, height=600)
        else:
            st.info("No stocks match your search.")
    else:
        st.info("⏳ No stock states available yet. The engine populates this after 09:20 IST.")

def _render_watchlist_and_chart() -> None:
    """New Tab — 🔭 Watchlist & Chart"""
    st.markdown("### 🔭 Watchlist & Chart")
    
    import json
    univ_path = os.path.join(_PROJECT_ROOT, "nifty50.json")
    try:
        if os.path.exists(univ_path):
            with open(univ_path, "r") as f:
                univ = json.load(f)
        else:
            univ = []
    except Exception:
        univ = []
        
    colA, colB = st.columns([1, 3])
    
    with colA:
        st.markdown("#### 📝 Your Watchlist")
        if univ:
            wl_df = pd.DataFrame(univ)
            st.dataframe(wl_df, use_container_width=True, hide_index=True, height=350)
        else:
            st.info("Watchlist is empty.")
            
        st.markdown("#### ➕ Add New Stock")
        new_sym = st.text_input("Symbol (e.g. RELIANCE)", key="wl_new_sym")
        new_sec_id = st.number_input("Security ID (Dhan)", min_value=1, step=1, key="wl_new_id")
        if st.button("Add to Watchlist", key="wl_add_btn"):
            if new_sym and new_sec_id > 1:
                if any(x.get("symbol") == new_sym.upper() for x in univ):
                    st.warning(f"⚠️ {new_sym.upper()} is already in the watchlist!")
                else:
                    univ.append({"security_id": int(new_sec_id), "symbol": new_sym.upper(), "exchange": "NSE_EQ"})
                    with open(univ_path, "w") as f:
                        json.dump(univ, f, indent=2)
                    st.success(f"✅ {new_sym.upper()} added!")
                    st.rerun()
            else:
                st.warning("Please provide a valid Symbol and Security ID.")

    with colB:
        st.markdown("#### 📊 Professional Chart")
        
        # Watchlist dropdown selection
        symbols_list = sorted([x.get("symbol") for x in univ])
        
        col1, col2 = st.columns([1, 1])
        with col1:
            selected_stock = st.selectbox("🎯 Select Stock", options=["NIFTY"] + symbols_list, index=0)
        with col2:
            chart_symbol = st.text_input("🔍 TradingView Symbol (e.g. NSE:RELIANCE)", value=f"NSE:{selected_stock}", key="tv_chart_sym")
            
        tradingview_html = f"""
        <div style="border: 1px solid rgba(0,212,255,0.15); border-radius:12px; overflow:hidden; margin-top:10px; height: 600px;">
        <div class="tradingview-widget-container" style="height:100%; width:100%;">
          <div id="tradingview_custom" style="height:100%; width:100%;"></div>
          <script type="text/javascript" src="https://s3.tradingview.com/tv.js"></script>
          <script type="text/javascript">
          new TradingView.widget({{
            "autosize": true,
            "symbol": "{chart_symbol}",
            "interval": "5",
            "timezone": "Asia/Kolkata",
            "theme": "dark",
            "style": "1",
            "locale": "en",
            "toolbar_bg": "#0a0a0f",
            "enable_publishing": false,
            "hide_side_toolbar": false,
            "allow_symbol_change": true,
            "save_image": true,
            "container_id": "tradingview_custom",
            "studies": [
              "RSI@tv-basicstudies",
              "MASimple@tv-basicstudies"
            ]
          }});
          </script>
        </div>
        </div>
        """
        st.components.v1.html(tradingview_html, height=620, scrolling=False)


def _render_active_positions(db: TradeDB) -> None:
    """Tab 2 — 📈 Active Positions."""
    st.markdown("### 📈 Active Positions")

    try:
        states = db.load_stock_states()
        active_states = [
            s for s in states
            if s.get("state") in ("TRADE_ENTERED", "PARTIAL_BOOKED")
        ]
    except Exception:
        active_states = []

    if active_states:
        rows = []
        total_mtm = 0.0
        for s in active_states:
            entry = s.get("entry_price", 0)
            ltp = s.get("ltp", entry)
            qty = s.get("remaining_qty", s.get("quantity", 0))
            side = s.get("side", "BUY")
            if side == "BUY":
                mtm = (ltp - entry) * qty
            else:
                mtm = (entry - ltp) * qty
            total_mtm += mtm
            rows.append({
                "Symbol": s.get("symbol", ""),
                "Side": "🟢 LONG" if side == "BUY" else "🔴 SHORT",
                "Qty": s.get("quantity", 0),
                "Remaining": qty,
                "Entry": f"₹{entry:.2f}",
                "LTP": f"₹{ltp:.2f}",
                "SL": f"₹{s.get('sl_price', 0):.2f}",
                "Target": f"₹{s.get('target_2rr', 0):.2f}",
                "MTM P&L": f"₹{mtm:+,.2f}",
                "Status": s.get("state", ""),
            })

        # Summary metrics
        col1, col2, col3 = st.columns(3)
        col1.metric("Open Positions", len(active_states))
        col2.metric("Total MTM P&L", f"₹{total_mtm:+,.2f}")
        col3.metric("Mode", "📝 PAPER" if settings.PAPER_TRADING else "💰 LIVE")

        df = pd.DataFrame(rows)
        st.dataframe(
            df,
            use_container_width=True,
            hide_index=True,
            column_config={
                "MTM P&L": st.column_config.TextColumn("MTM P&L"),
            },
        )
    else:
        st.info("🚫 No active positions right now.")


def _render_trade_history(db: TradeDB) -> None:
    """Tab 3 — 📋 Trade History."""
    st.markdown("### 📋 Trade History")

    col1, col2 = st.columns([1, 3])
    with col1:
        days = st.selectbox("Period", [7, 14, 30, 60, 90], index=2)

    try:
        trades = db.get_trade_history(days=days)  # type: ignore[arg-type]
    except Exception:
        trades = []

    if trades:
        df = pd.DataFrame(trades)

        # --- Daily P&L summary ---
        st.markdown("#### 📊 Daily P&L Summary")
        total_pnl = sum(t.get("pnl", 0) for t in trades)
        wins = sum(1 for t in trades if t.get("pnl", 0) > 0)
        losses = sum(1 for t in trades if t.get("pnl", 0) < 0)
        breakeven = len(trades) - wins - losses

        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Total Trades", len(trades))
        c2.metric("Total P&L", f"₹{total_pnl:+,.2f}")
        c3.metric("Wins", f"{wins} ✅")
        c4.metric("Losses", f"{losses} ❌")
        c5.metric("Win Rate", f"{(wins / len(trades) * 100):.1f}%" if trades else "0%")

        # --- Full table ---
        st.markdown("#### 📄 Full Trade Log")
        display_cols = [
            c for c in [
                "trade_id", "symbol", "side", "quantity", "entry_price",
                "exit_price", "pnl", "sl_price", "target_price",
                "entry_time", "exit_time", "exit_reason", "state",
            ] if c in df.columns
        ]
        if display_cols:
            st.dataframe(df[display_cols], use_container_width=True, hide_index=True, height=500)
        else:
            st.dataframe(df, use_container_width=True, hide_index=True, height=500)
    else:
        st.info(f"📭 No trades found in the last {days} days.")


def _render_export(db: TradeDB) -> None:
    """Tab 4 — ⬇️ Export."""
    st.markdown("### ⬇️ Export Trade Data")

    col1, col2 = st.columns([1, 3])
    with col1:
        export_date = st.date_input("Select date", value=date.today(), key="export_date")

    date_str = export_date.strftime("%Y-%m-%d")  # type: ignore[union-attr]

    if st.button("📥 Download CSV", key="export_btn"):
        try:
            csv_path = db.export_csv(date_str)
            if os.path.exists(csv_path):
                with open(csv_path, "r", encoding="utf-8") as f:
                    csv_data = f.read()
                st.download_button(
                    label="💾 Save CSV File",
                    data=csv_data,
                    file_name=f"orb_trades_{date_str}.csv",
                    mime="text/csv",
                    key="download_csv_btn",
                )
                st.success(f"✅ CSV generated for {date_str}")
            else:
                st.warning(f"⚠️ No trades found for {date_str}.")
        except Exception as exc:
            st.error(f"❌ Export failed: {exc}")


def _render_system_status(db: TradeDB) -> None:
    """Tab 5 — ⚙️ System Status."""
    st.markdown("### ⚙️ System Status")

    # --- Paper trading mode ---
    if settings.PAPER_TRADING:
        st.markdown('<span class="paper-badge">📝 PAPER TRADING MODE</span>', unsafe_allow_html=True)
    else:
        st.markdown(
            '<span style="display:inline-block;padding:6px 18px;background:rgba(0,255,136,0.15);'
            'border:2px solid #00ff88;border-radius:20px;color:#00ff88;font-weight:700;'
            'font-size:0.95rem;letter-spacing:1px;">💰 LIVE TRADING MODE</span>',
            unsafe_allow_html=True,
        )
    st.markdown("")

    # --- Status indicators ---
    col1, col2, col3, col4 = st.columns(4)

    # Token health
    token_ok = False
    try:
        stored_token = db.get_active_token()
        token_ok = bool(stored_token)
    except Exception:
        pass
    dot_cls = "dot-green" if token_ok else "dot-red"
    label = "Healthy" if token_ok else "Missing / Expired"
    col1.markdown(f'<span class="{dot_cls}"></span> **Token**: {label}', unsafe_allow_html=True)

    # Server time
    col2.markdown(f'<span class="dot-green"></span> **Server Time**: {_now_ist().strftime("%H:%M:%S IST")}', unsafe_allow_html=True)

    # Uptime
    uptime_secs = (datetime.now(IST) - _BOOT_TIME).total_seconds()
    h, rem = divmod(int(uptime_secs), 3600)
    m, s = divmod(rem, 60)
    col3.markdown(f'<span class="dot-green"></span> **Dashboard Uptime**: {h}h {m}m {s}s', unsafe_allow_html=True)

    # Last heartbeat (approximation — we use current refresh)
    col4.markdown(
        f'<span class="dot-green"></span> **Last Refresh**: {_now_ist().strftime("%H:%M:%S")}',
        unsafe_allow_html=True,
    )

    st.markdown("---")

    # --- Token update ---
    st.markdown("#### 🔑 Update Dhan Access Token")
    st.caption(
        "Paste your daily Dhan access token below. The main engine will pick it up "
        "automatically before the next trading session."
    )
    new_token = st.text_input(
        "New Access Token",
        type="password",
        placeholder="Paste token here…",
        key="token_input",
    )
    if st.button("🔄 Update Token", key="update_token_btn"):
        if new_token and len(new_token) > 10:
            try:
                db.set_active_token(new_token)
                st.success("✅ Token saved! The engine will use it on the next session.")
                db.log_system_event("TOKEN_UPDATED", f"Token updated via dashboard at {_now_ist().isoformat()}")
            except Exception as exc:
                st.error(f"❌ Failed to save token: {exc}")
        else:
            st.warning("⚠️ Please paste a valid access token (min 10 characters).")

    st.markdown("---")

    # --- System info ---
    st.markdown("#### ℹ️ System Info")
    info_data = {
        "Version": APP_VERSION,
        "Database Path": settings.DB_PATH,
        "Paper Trading": str(settings.PAPER_TRADING),
        "Base Capital": f"₹{settings.BASE_CAPITAL:,.0f}",
        "Leverage": f"{settings.LEVERAGE}x",
        "Max Range %": f"{settings.MAX_RANGE_PCT}%",
        "Min Stock Price": f"₹{settings.MIN_STOCK_PRICE:.0f}",
        "Market Open": str(settings.MARKET_OPEN),
        "ORB Close": str(settings.ORB_CANDLE_CLOSE),
        "Scan Cutoff": str(settings.SCAN_CUTOFF),
        "Square-off": str(settings.SQUARE_OFF_TIME),
    }
    st.table(pd.DataFrame(info_data.items(), columns=["Setting", "Value"]))


# ---------------------------------------------------------------------------
# Main dashboard layout
# ---------------------------------------------------------------------------

def main() -> None:
    """Render the full dashboard."""
    # --- Auth gate ---
    if not _check_password():
        return

    # --- Header ---
    st.markdown('<div class="neon-title">🎯 ORB INTRADAY TRADER</div>', unsafe_allow_html=True)
    st.markdown(
        '<div class="neon-subtitle">Opening Range Breakout · NIFTY 50 · Automated</div>',
        unsafe_allow_html=True,
    )

    # --- DB connection ---
    db = _get_db()

    # --- Top metrics bar ---
    try:
        today_trades = db.get_today_trades()
    except Exception:
        today_trades = []

    total_pnl = sum(t.get("pnl", 0) for t in today_trades)
    wins = sum(1 for t in today_trades if t.get("pnl", 0) > 0)
    losses = sum(1 for t in today_trades if t.get("pnl", 0) < 0)

    try:
        states = db.load_stock_states()
        active_count = sum(
            1 for s in states if s.get("state") in ("TRADE_ENTERED", "PARTIAL_BOOKED")
        )
    except Exception:
        states = []
        active_count = 0

    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Today's Trades", len(today_trades))
    m2.metric("Active Positions", active_count)
    m3.metric("Day P&L", f"₹{total_pnl:+,.2f}")
    m4.metric("Win / Loss", f"{wins}W / {losses}L")
    m5.metric("Mode", "📝 Paper" if settings.PAPER_TRADING else "💰 Live")

    st.markdown("---")

    # --- Tabs ---
    tab1, tab2, tab3, tab4, tab5, tab6 = st.tabs([
        "📡 Live Scanner",
        "🔭 Watchlist & Chart",
        "📈 Active Positions",
        "📋 Trade History",
        "⬇️ Export",
        "⚙️ System Status",
    ])

    with tab1:
        _render_live_scanner(db)
    with tab2:
        _render_watchlist_and_chart()
    with tab3:
        _render_active_positions(db)
    with tab4:
        _render_trade_history(db)
    with tab5:
        _render_export(db)
    with tab6:
        _render_system_status(db)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    main()
else:
    # Streamlit runs the file directly — not via __main__
    main()
