"""
ORB Intraday Trading System - Database Module
===============================================
Thread-safe SQLite wrapper for trade logging, candle storage,
crash-recovery state, system events, and token persistence.

All write operations are serialised through a threading.Lock so the
module is safe for use from the strategy engine, the FastAPI webhook
server, and the Streamlit dashboard simultaneously.
"""

import json
import logging
import os
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

import pytz

from config import IST

logger = logging.getLogger(__name__)


class TradeDB:
    """
    SQLite-backed persistence layer for the ORB system.

    Tables
    ------
    trades           – full lifecycle of every trade
    candles           – raw OHLCV candle data
    daily_stock_state – serialised StockState per security per day
    system_events     – audit log of system-level events
    token_store       – latest Dhan access token (dashboard paste)
    """

    def __init__(self, db_path: str) -> None:
        """
        Initialise the database, creating the directory and all tables
        if they don't exist yet.

        Args:
            db_path: Relative or absolute path to the SQLite file.
        """
        self._db_path = db_path
        self._lock = threading.Lock()

        # Ensure parent directory exists
        db_dir = Path(db_path).parent
        db_dir.mkdir(parents=True, exist_ok=True)

        self._create_tables()
        logger.info("TradeDB initialised at %s", self._db_path)

    # ------------------------------------------------------------------
    # Connection helper
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        """Return a new connection with row-factory set to dict."""
        conn = sqlite3.connect(self._db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    # ------------------------------------------------------------------
    # Schema creation
    # ------------------------------------------------------------------

    def _create_tables(self) -> None:
        """Create all tables if they don't already exist."""
        with self._lock:
            conn = self._connect()
            try:
                conn.executescript("""
                    CREATE TABLE IF NOT EXISTS trades (
                        id              INTEGER PRIMARY KEY AUTOINCREMENT,
                        date            TEXT    NOT NULL,
                        security_id     INTEGER NOT NULL,
                        symbol          TEXT    NOT NULL,
                        side            TEXT    NOT NULL,
                        quantity         INTEGER NOT NULL DEFAULT 0,
                        entry_price     REAL    NOT NULL DEFAULT 0.0,
                        sl_price        REAL    NOT NULL DEFAULT 0.0,
                        target_price    REAL    NOT NULL DEFAULT 0.0,
                        exit_price      REAL    DEFAULT NULL,
                        pnl             REAL    DEFAULT NULL,
                        status          TEXT    NOT NULL DEFAULT 'OPEN',
                        entry_time      TEXT    DEFAULT NULL,
                        exit_time       TEXT    DEFAULT NULL,
                        order_id        TEXT    DEFAULT NULL,
                        partial_order_id TEXT   DEFAULT NULL,
                        remaining_qty   INTEGER DEFAULT 0
                    );

                    CREATE TABLE IF NOT EXISTS candles (
                        id          INTEGER PRIMARY KEY AUTOINCREMENT,
                        date        TEXT    NOT NULL,
                        security_id INTEGER NOT NULL,
                        symbol      TEXT    NOT NULL,
                        timeframe   TEXT    NOT NULL DEFAULT '5min',
                        open        REAL    NOT NULL,
                        high        REAL    NOT NULL,
                        low         REAL    NOT NULL,
                        close       REAL    NOT NULL,
                        volume      INTEGER NOT NULL DEFAULT 0,
                        timestamp   TEXT    NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS daily_stock_state (
                        id          INTEGER PRIMARY KEY AUTOINCREMENT,
                        date        TEXT    NOT NULL,
                        security_id INTEGER NOT NULL,
                        state_json  TEXT    NOT NULL,
                        updated_at  TEXT    NOT NULL,
                        UNIQUE(date, security_id)
                    );

                    CREATE TABLE IF NOT EXISTS system_events (
                        id          INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp   TEXT    NOT NULL,
                        event_type  TEXT    NOT NULL,
                        details     TEXT    DEFAULT ''
                    );

                    CREATE TABLE IF NOT EXISTS token_store (
                        id          INTEGER PRIMARY KEY CHECK (id = 1),
                        token       TEXT    NOT NULL,
                        updated_at  TEXT    NOT NULL
                    );

                    CREATE INDEX IF NOT EXISTS idx_trades_date
                        ON trades(date);
                    CREATE INDEX IF NOT EXISTS idx_trades_security_date
                        ON trades(security_id, date);
                    CREATE INDEX IF NOT EXISTS idx_candles_security_date
                        ON candles(security_id, date);
                    CREATE INDEX IF NOT EXISTS idx_stock_state_date
                        ON daily_stock_state(date);
                    CREATE INDEX IF NOT EXISTS idx_events_timestamp
                        ON system_events(timestamp);
                """)
                conn.commit()
            finally:
                conn.close()

    # ------------------------------------------------------------------
    # Trade operations
    # ------------------------------------------------------------------

    def log_trade(self, trade: dict) -> int:
        """
        Insert a new trade record and return its auto-generated ID.

        Args:
            trade: Dict with keys matching trades table columns.
                   At minimum: date, security_id, symbol, side.

        Returns:
            The integer row ID of the new trade.
        """
        cols = [
            "date", "security_id", "symbol", "side", "quantity",
            "entry_price", "sl_price", "target_price", "exit_price",
            "pnl", "status", "entry_time", "exit_time",
            "order_id", "partial_order_id", "remaining_qty",
        ]
        present_cols = [c for c in cols if c in trade]
        placeholders = ", ".join("?" for _ in present_cols)
        col_names = ", ".join(present_cols)
        values = [trade[c] for c in present_cols]

        with self._lock:
            conn = self._connect()
            try:
                cursor = conn.execute(
                    f"INSERT INTO trades ({col_names}) VALUES ({placeholders})",
                    values,
                )
                conn.commit()
                trade_id = cursor.lastrowid
                logger.info(
                    "Logged trade #%d: %s %s %s",
                    trade_id, trade.get("side"), trade.get("symbol"), trade.get("status"),
                )
                return trade_id  # type: ignore[return-value]
            finally:
                conn.close()

    def update_trade(self, trade_id: int, updates: dict) -> None:
        """
        Update specific columns of an existing trade.

        Args:
            trade_id: The trade row ID.
            updates:  Dict of column → new value.
        """
        if not updates:
            return
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [trade_id]

        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    f"UPDATE trades SET {set_clause} WHERE id = ?", values
                )
                conn.commit()
                logger.debug("Updated trade #%d: %s", trade_id, updates)
            finally:
                conn.close()

    def get_today_trades(self) -> list[dict]:
        """Return all trades for today's date (IST)."""
        today_str = datetime.now(IST).strftime("%Y-%m-%d")
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM trades WHERE date = ? ORDER BY id",
                (today_str,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_trade_history(self, days: int = 30) -> list[dict]:
        """
        Return trades from the last *days* calendar days.

        Args:
            days: Look-back window in calendar days.

        Returns:
            List of trade dicts, most recent first.
        """
        from datetime import timedelta
        cutoff = (datetime.now(IST) - timedelta(days=days)).strftime("%Y-%m-%d")
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM trades WHERE date >= ? ORDER BY id DESC",
                (cutoff,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def has_traded_today(self, security_id: int) -> bool:
        """
        Check if a trade already exists for the given security today.

        Args:
            security_id: Dhan security ID.

        Returns:
            True if at least one trade row exists for this security today.
        """
        today_str = datetime.now(IST).strftime("%Y-%m-%d")
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM trades "
                "WHERE security_id = ? AND date = ?",
                (security_id, today_str),
            ).fetchone()
            return dict(row)["cnt"] > 0
        finally:
            conn.close()

    def export_csv(self, date_str: str) -> str:
        """
        Export all trades for a given date to a CSV file.

        Args:
            date_str: Date string in ``YYYY-MM-DD`` format.

        Returns:
            Absolute path to the generated CSV file.
        """
        import csv

        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM trades WHERE date = ? ORDER BY id",
                (date_str,),
            ).fetchall()
            trade_dicts = [dict(r) for r in rows]
        finally:
            conn.close()

        export_dir = Path("data")
        export_dir.mkdir(parents=True, exist_ok=True)
        csv_path = export_dir / f"trades_{date_str}.csv"

        if not trade_dicts:
            # Write an empty CSV with headers only
            headers = [
                "id", "date", "security_id", "symbol", "side", "quantity",
                "entry_price", "sl_price", "target_price", "exit_price",
                "pnl", "status", "entry_time", "exit_time",
                "order_id", "partial_order_id", "remaining_qty",
            ]
        else:
            headers = list(trade_dicts[0].keys())

        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=headers)
            writer.writeheader()
            writer.writerows(trade_dicts)

        abs_path = str(csv_path.resolve())
        logger.info("Exported %d trades to %s", len(trade_dicts), abs_path)
        return abs_path

    # ------------------------------------------------------------------
    # Candle operations
    # ------------------------------------------------------------------

    def log_candle(self, candle: dict) -> None:
        """
        Insert a candle record.

        Args:
            candle: Dict with keys: date, security_id, symbol, timeframe,
                    open, high, low, close, volume, timestamp.
        """
        cols = [
            "date", "security_id", "symbol", "timeframe",
            "open", "high", "low", "close", "volume", "timestamp",
        ]
        values = [candle.get(c) for c in cols]
        placeholders = ", ".join("?" for _ in cols)
        col_names = ", ".join(cols)

        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    f"INSERT INTO candles ({col_names}) VALUES ({placeholders})",
                    values,
                )
                conn.commit()
            finally:
                conn.close()

    # ------------------------------------------------------------------
    # System events
    # ------------------------------------------------------------------

    def log_system_event(self, event: str, details: str = "") -> None:
        """
        Record a system-level event (startup, shutdown, error, etc.).

        Args:
            event:   Short event type label.
            details: Free-text details.
        """
        ts = datetime.now(IST).isoformat()
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT INTO system_events (timestamp, event_type, details) "
                    "VALUES (?, ?, ?)",
                    (ts, event, details),
                )
                conn.commit()
            finally:
                conn.close()

    # ------------------------------------------------------------------
    # Crash-recovery stock state
    # ------------------------------------------------------------------

    def save_stock_state(self, state: dict) -> None:
        """
        Persist a stock's runtime state as a JSON blob for crash recovery.

        Uses ``INSERT OR REPLACE`` keyed on (date, security_id).

        Args:
            state: Dict with at least ``security_id`` key.  The full dict
                   is serialised to JSON.
        """
        today_str = datetime.now(IST).strftime("%Y-%m-%d")
        ts = datetime.now(IST).isoformat()
        state_json = json.dumps(state, default=str)

        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO daily_stock_state "
                    "(date, security_id, state_json, updated_at) "
                    "VALUES (?, ?, ?, ?)",
                    (today_str, state["security_id"], state_json, ts),
                )
                conn.commit()
            finally:
                conn.close()

    def load_stock_states(self) -> list[dict]:
        """
        Load all saved stock states for today (for crash recovery).

        Returns:
            List of de-serialised state dicts.
        """
        today_str = datetime.now(IST).strftime("%Y-%m-%d")
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT state_json FROM daily_stock_state WHERE date = ?",
                (today_str,),
            ).fetchall()
            states: list[dict] = []
            for row in rows:
                try:
                    states.append(json.loads(dict(row)["state_json"]))
                except (json.JSONDecodeError, KeyError):
                    logger.warning("Corrupt state row skipped")
            return states
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Token store (for dashboard paste feature)
    # ------------------------------------------------------------------

    def get_active_token(self) -> str | None:
        """
        Retrieve the most recently stored access token.

        Returns:
            The token string, or None if no token has been stored.
        """
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT token FROM token_store WHERE id = 1"
            ).fetchone()
            if row:
                return dict(row)["token"]
            return None
        finally:
            conn.close()

    def set_active_token(self, token: str) -> None:
        """
        Store or update the active access token.

        Args:
            token: The new Dhan access token string.
        """
        ts = datetime.now(IST).isoformat()
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO token_store (id, token, updated_at) "
                    "VALUES (1, ?, ?)",
                    (token, ts),
                )
                conn.commit()
                logger.info("Access token updated in token_store")
            finally:
                conn.close()
