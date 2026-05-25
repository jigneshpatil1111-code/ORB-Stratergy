"""
webhook.py — FastAPI webhook listener for TradingView signal integration.

Endpoints:
  POST /webhook   — Receives trading signals, validates secret, logs to DB
  GET  /health    — System health check (uptime, version, status)
  GET  /api/status — Current strategy state (active trades, positions)

All times in IST (Asia/Kolkata). Currency in INR (₹).
"""

import logging
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Header, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, validator

from config import settings
from database import TradeDB

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
IST = timezone(timedelta(hours=5, minutes=30))
APP_VERSION = "1.0.0"
_START_TIME = time.monotonic()
_START_DATETIME = datetime.now(IST)

logger = logging.getLogger("orb.webhook")

# ---------------------------------------------------------------------------
# Pydantic models for request / response validation
# ---------------------------------------------------------------------------

class WebhookSignal(BaseModel):
    """Incoming TradingView alert payload."""
    symbol: str = Field(..., min_length=1, max_length=30, description="NSE symbol, e.g. HDFCBANK")
    action: str = Field(..., description="Signal action: BUY, SELL, EXIT")
    price: float = Field(..., gt=0, description="Trigger price in INR")
    timeframe: Optional[str] = Field(None, description="Chart timeframe, e.g. 5m")
    strategy_name: Optional[str] = Field(None, description="Name of the originating strategy")
    message: Optional[str] = Field(None, max_length=500, description="Free-text alert message")
    timestamp: Optional[str] = Field(None, description="ISO-8601 timestamp from TradingView")

    @validator("action")
    def action_must_be_valid(cls, v: str) -> str:  # noqa: N805
        allowed = {"BUY", "SELL", "EXIT", "LONG", "SHORT", "CLOSE"}
        upper = v.strip().upper()
        if upper not in allowed:
            raise ValueError(f"action must be one of {allowed}")
        return upper

    @validator("symbol")
    def symbol_upper(cls, v: str) -> str:  # noqa: N805
        return v.strip().upper()


class HealthResponse(BaseModel):
    status: str
    version: str
    uptime_seconds: float
    uptime_human: str
    started_at: str
    server_time: str
    paper_trading: bool


class StatusResponse(BaseModel):
    active_trades_count: int
    today_trades_count: int
    today_pnl: float
    paper_trading: bool
    token_healthy: bool
    server_time: str


class WebhookAck(BaseModel):
    received: bool
    signal_id: int
    message: str

# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------

app = FastAPI(
    title="ORB Intraday Webhook",
    description="Receives TradingView alerts and exposes system status for the ORB strategy engine.",
    version=APP_VERSION,
)

# Allow CORS for dashboard / external tools
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Shared state — set by main.py at startup
# ---------------------------------------------------------------------------
# These are injected by the orchestrator so the webhook can read live state
# without importing heavy strategy objects at module level.
_strategy_engine: Any = None
_broker: Any = None
_db: Optional[TradeDB] = None


def set_strategy_engine(engine: Any) -> None:
    """Called by main.py to inject the live strategy engine reference."""
    global _strategy_engine
    _strategy_engine = engine
    logger.info("Webhook: strategy engine reference set.")


def set_broker(broker: Any) -> None:
    """Called by main.py to inject the live broker reference."""
    global _broker
    _broker = broker
    logger.info("Webhook: broker reference set.")


def set_database(db: TradeDB) -> None:
    """Called by main.py to inject the shared TradeDB instance."""
    global _db
    _db = db
    logger.info("Webhook: database reference set.")


def _get_db() -> TradeDB:
    """Return the shared DB, falling back to a new instance if needed."""
    global _db
    if _db is None:
        _db = TradeDB(settings.DB_PATH)
    return _db

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _uptime_human(seconds: float) -> str:
    """Convert seconds to a human-readable string like '2h 14m 7s'."""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    parts: list[str] = []
    if h:
        parts.append(f"{h}h")
    if m:
        parts.append(f"{m}m")
    parts.append(f"{s}s")
    return " ".join(parts)


def _now_ist() -> datetime:
    return datetime.now(IST)

# ---------------------------------------------------------------------------
# POST /webhook
# ---------------------------------------------------------------------------

@app.post("/webhook", response_model=WebhookAck, status_code=200)
async def receive_webhook(
    signal: WebhookSignal,
    request: Request,
    x_webhook_secret: Optional[str] = Header(None),
) -> WebhookAck:
    """
    Receive a TradingView (or compatible) alert.

    The caller must pass the shared secret via the ``X-Webhook-Secret`` header.
    The signal is validated, logged to the database, and acknowledged.
    """
    # --- Authenticate ---
    expected_secret = settings.WEBHOOK_SECRET
    if expected_secret:
        if x_webhook_secret is None or x_webhook_secret != expected_secret:
            logger.warning(
                "Webhook auth failed from %s — bad or missing secret.",
                request.client.host if request.client else "unknown",
            )
            raise HTTPException(status_code=401, detail="Invalid or missing X-Webhook-Secret header.")

    logger.info(
        "Webhook signal received: symbol=%s action=%s price=%.2f",
        signal.symbol,
        signal.action,
        signal.price,
    )

    # --- Persist to database ---
    db = _get_db()
    try:
        signal_record = {
            "source": "tradingview_webhook",
            "symbol": signal.symbol,
            "action": signal.action,
            "price": signal.price,
            "timeframe": signal.timeframe or "",
            "strategy_name": signal.strategy_name or "",
            "message": signal.message or "",
            "signal_timestamp": signal.timestamp or _now_ist().isoformat(),
            "received_at": _now_ist().isoformat(),
            "client_ip": request.client.host if request.client else "unknown",
        }

        # Log as a system event (the dedicated signal table may be added later)
        db.log_system_event(
            event="WEBHOOK_SIGNAL",
            details=(
                f"symbol={signal.symbol} action={signal.action} "
                f"price={signal.price} tf={signal.timeframe}"
            ),
        )

        # Also store as a candle-like record for auditing
        signal_id_hash = abs(hash(f"{signal.symbol}{signal.action}{signal.price}{time.time()}")) % 10**9
        db.log_candle({
            "security_id": signal_id_hash,
            "symbol": signal.symbol,
            "timestamp": _now_ist().isoformat(),
            "open": signal.price,
            "high": signal.price,
            "low": signal.price,
            "close": signal.price,
            "volume": 0,
            "source": "webhook",
        })

        logger.info("Webhook signal persisted. hash_id=%d", signal_id_hash)

        return WebhookAck(
            received=True,
            signal_id=signal_id_hash,
            message=f"Signal for {signal.symbol} ({signal.action}) acknowledged.",
        )

    except Exception as exc:
        logger.exception("Failed to persist webhook signal: %s", exc)
        raise HTTPException(status_code=500, detail="Internal error while processing signal.") from exc

# ---------------------------------------------------------------------------
# GET /health
# ---------------------------------------------------------------------------

@app.get("/health", response_model=HealthResponse)
async def health_check() -> HealthResponse:
    """Lightweight health-check used by Docker HEALTHCHECK and monitoring."""
    uptime = time.monotonic() - _START_TIME
    return HealthResponse(
        status="ok",
        version=APP_VERSION,
        uptime_seconds=round(uptime, 2),
        uptime_human=_uptime_human(uptime),
        started_at=_START_DATETIME.isoformat(),
        server_time=_now_ist().isoformat(),
        paper_trading=settings.PAPER_TRADING,
    )

# ---------------------------------------------------------------------------
# GET /api/status
# ---------------------------------------------------------------------------

@app.get("/api/status", response_model=StatusResponse)
async def api_status() -> StatusResponse:
    """
    Returns a snapshot of the strategy engine state:
      - number of active trades
      - today's completed trades
      - cumulative P&L
      - token health
    """
    db = _get_db()

    # Active trades from strategy engine (if available)
    active_count = 0
    if _strategy_engine is not None:
        try:
            active_trades = _strategy_engine.get_active_trades()
            active_count = len(active_trades)
        except Exception as exc:
            logger.warning("Could not read active trades from engine: %s", exc)

    # Today's completed trades from DB
    today_trades: list[dict] = []
    today_pnl = 0.0
    try:
        today_trades = db.get_today_trades()
        today_pnl = sum(t.get("pnl", 0.0) for t in today_trades)
    except Exception as exc:
        logger.warning("Could not read today trades from DB: %s", exc)

    # Token health
    token_ok = True
    if _broker is not None:
        try:
            token_ok = _broker.check_token_health()
        except Exception:
            token_ok = False

    return StatusResponse(
        active_trades_count=active_count,
        today_trades_count=len(today_trades),
        today_pnl=round(today_pnl, 2),
        paper_trading=settings.PAPER_TRADING,
        token_healthy=token_ok,
        server_time=_now_ist().isoformat(),
    )

# ---------------------------------------------------------------------------
# Catch-all error handler
# ---------------------------------------------------------------------------

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> Response:
    logger.exception("Unhandled error on %s %s: %s", request.method, request.url.path, exc)
    from fastapi.responses import JSONResponse
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error. Check logs for details."},
    )
