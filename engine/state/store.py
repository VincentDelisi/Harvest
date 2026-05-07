"""Persistent state for the trading engine — SQLite-backed.

Tables:
  trades        — every spread we've opened (open + closed). Source of truth for P&L.
  daily_pnl     — derived view written at session close for fast querying.
  kill_switch   — single-row table holding the current halt flag + reason.
  engine_meta   — generic key/value for runtime metadata.

This is the ONLY module that owns trade lifecycle state. Other modules
read/write through this interface — no direct SQL elsewhere.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

from engine.utils.config import CONFIG
from engine.utils.logging import get_logger

log = get_logger(__name__)


@dataclass
class TradeRecord:
    """One credit-spread trade. `pnl` is filled when the trade closes."""
    trade_id: str                  # internal UUID we generate
    underlying: str                # SPY / QQQ / IWM
    direction: str                 # "PUT" or "CALL"
    short_strike: float
    long_strike: float
    width: float
    expiration: str                # YYYY-MM-DD
    short_symbol: str              # OCC
    long_symbol: str               # OCC
    quantity: int
    credit_received: float         # per-spread, from fill or limit
    open_order_id: Optional[str] = None
    open_status: str = "PENDING"   # PENDING/FILLED/CANCELLED/REJECTED
    opened_at: Optional[str] = None  # ISO-8601 ET
    close_order_id: Optional[str] = None
    close_status: Optional[str] = None
    closed_at: Optional[str] = None
    close_reason: Optional[str] = None  # PROFIT_TARGET / STOP_LOSS / TESTED_STRIKE / TIME_STOP / EXPIRED / MANUAL
    debit_paid: Optional[float] = None  # cost to close, per-spread
    pnl: Optional[float] = None         # net P&L in $ per-spread × quantity, after close
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class KillSwitchState:
    active: bool
    reason: Optional[str]
    triggered_at: Optional[str]


class StateStore:
    """SQLite wrapper for engine state."""

    SCHEMA_VERSION = 1

    def __init__(self, db_path: str | None = None) -> None:
        path = Path(db_path or CONFIG.sqlite_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = str(path)
        self.conn = sqlite3.connect(self.path, detect_types=sqlite3.PARSE_DECLTYPES)
        self.conn.row_factory = sqlite3.Row
        self._migrate()

    # ─────────────────────── schema ──────────────────────────────────────

    def _migrate(self) -> None:
        c = self.conn.cursor()
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS trades (
                trade_id        TEXT PRIMARY KEY,
                underlying      TEXT NOT NULL,
                direction       TEXT NOT NULL,
                short_strike    REAL NOT NULL,
                long_strike     REAL NOT NULL,
                width           REAL NOT NULL,
                expiration      TEXT NOT NULL,
                short_symbol    TEXT NOT NULL,
                long_symbol     TEXT NOT NULL,
                quantity        INTEGER NOT NULL,
                credit_received REAL NOT NULL,
                open_order_id   TEXT,
                open_status     TEXT NOT NULL DEFAULT 'PENDING',
                opened_at       TEXT,
                close_order_id  TEXT,
                close_status    TEXT,
                closed_at       TEXT,
                close_reason    TEXT,
                debit_paid      REAL,
                pnl             REAL,
                extra_json      TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_trades_open_status ON trades(open_status);
            CREATE INDEX IF NOT EXISTS idx_trades_close_status ON trades(close_status);
            CREATE INDEX IF NOT EXISTS idx_trades_underlying ON trades(underlying);
            CREATE INDEX IF NOT EXISTS idx_trades_opened_at ON trades(opened_at);

            CREATE TABLE IF NOT EXISTS kill_switch (
                id            INTEGER PRIMARY KEY CHECK (id = 1),
                active        INTEGER NOT NULL DEFAULT 0,
                reason        TEXT,
                triggered_at  TEXT
            );
            INSERT OR IGNORE INTO kill_switch(id, active) VALUES (1, 0);

            CREATE TABLE IF NOT EXISTS engine_meta (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS daily_pnl (
                date         TEXT PRIMARY KEY,
                realized_pnl REAL NOT NULL DEFAULT 0,
                trades       INTEGER NOT NULL DEFAULT 0,
                wins         INTEGER NOT NULL DEFAULT 0,
                losses       INTEGER NOT NULL DEFAULT 0,
                starting_equity REAL,
                ending_equity   REAL
            );
            """
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ─────────────────────── trades ──────────────────────────────────────

    def insert_trade(self, t: TradeRecord) -> None:
        self.conn.execute(
            """
            INSERT INTO trades(
                trade_id, underlying, direction, short_strike, long_strike, width,
                expiration, short_symbol, long_symbol, quantity, credit_received,
                open_order_id, open_status, opened_at,
                close_order_id, close_status, closed_at, close_reason, debit_paid, pnl,
                extra_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                t.trade_id, t.underlying, t.direction, t.short_strike, t.long_strike,
                t.width, t.expiration, t.short_symbol, t.long_symbol, t.quantity,
                t.credit_received, t.open_order_id, t.open_status, t.opened_at,
                t.close_order_id, t.close_status, t.closed_at, t.close_reason, t.debit_paid, t.pnl,
                json.dumps(t.extra) if t.extra else None,
            ),
        )
        self.conn.commit()

    def update_open_status(
        self, trade_id: str, *, open_status: str, opened_at: Optional[str] = None,
        credit_received: Optional[float] = None,
    ) -> None:
        sets = ["open_status = ?"]
        params: list[Any] = [open_status]
        if opened_at is not None:
            sets.append("opened_at = ?")
            params.append(opened_at)
        if credit_received is not None:
            sets.append("credit_received = ?")
            params.append(credit_received)
        params.append(trade_id)
        self.conn.execute(f"UPDATE trades SET {', '.join(sets)} WHERE trade_id = ?", params)
        self.conn.commit()

    def record_close(
        self,
        trade_id: str,
        *,
        close_order_id: Optional[str],
        close_status: str,
        closed_at: str,
        close_reason: str,
        debit_paid: float,
    ) -> Optional[TradeRecord]:
        """Mark a trade closed and compute P&L. Returns the updated record."""
        row = self.get_trade(trade_id)
        if row is None:
            log.warning("record_close: trade %s not found", trade_id)
            return None
        # P&L per spread = credit - debit; total = (credit - debit) * 100 * quantity
        per_spread = (row.credit_received - debit_paid)
        pnl = round(per_spread * 100.0 * row.quantity, 2)
        self.conn.execute(
            """
            UPDATE trades SET
                close_order_id = ?, close_status = ?, closed_at = ?,
                close_reason = ?, debit_paid = ?, pnl = ?
            WHERE trade_id = ?
            """,
            (close_order_id, close_status, closed_at, close_reason, debit_paid, pnl, trade_id),
        )
        self.conn.commit()
        return self.get_trade(trade_id)

    def get_trade(self, trade_id: str) -> Optional[TradeRecord]:
        row = self.conn.execute(
            "SELECT * FROM trades WHERE trade_id = ?", (trade_id,)
        ).fetchone()
        return self._row_to_trade(row) if row else None

    def open_trades(self) -> list[TradeRecord]:
        """All trades that are filled but not yet closed."""
        rows = self.conn.execute(
            """
            SELECT * FROM trades
            WHERE open_status = 'FILLED' AND (close_status IS NULL OR close_status NOT IN ('FILLED', 'EXPIRED'))
            """
        ).fetchall()
        return [self._row_to_trade(r) for r in rows]

    def pending_trades(self) -> list[TradeRecord]:
        """Trades whose open order is still working."""
        rows = self.conn.execute(
            "SELECT * FROM trades WHERE open_status = 'PENDING'"
        ).fetchall()
        return [self._row_to_trade(r) for r in rows]

    def trades_opened_on(self, on: date) -> list[TradeRecord]:
        rows = self.conn.execute(
            "SELECT * FROM trades WHERE date(opened_at) = ?", (on.isoformat(),)
        ).fetchall()
        return [self._row_to_trade(r) for r in rows]

    def trades_closed_on(self, on: date) -> list[TradeRecord]:
        rows = self.conn.execute(
            "SELECT * FROM trades WHERE date(closed_at) = ?", (on.isoformat(),)
        ).fetchall()
        return [self._row_to_trade(r) for r in rows]

    def total_trades(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]

    def filled_trade_count(self) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM trades WHERE open_status = 'FILLED'"
        ).fetchone()[0]

    def recent_closed_trades(self, n: int = 5) -> list[TradeRecord]:
        rows = self.conn.execute(
            """
            SELECT * FROM trades
            WHERE close_status IS NOT NULL AND pnl IS NOT NULL
            ORDER BY closed_at DESC LIMIT ?
            """,
            (n,),
        ).fetchall()
        return [self._row_to_trade(r) for r in rows]

    @staticmethod
    def _row_to_trade(row: sqlite3.Row) -> TradeRecord:
        extra = json.loads(row["extra_json"]) if row["extra_json"] else {}
        return TradeRecord(
            trade_id=row["trade_id"],
            underlying=row["underlying"],
            direction=row["direction"],
            short_strike=row["short_strike"],
            long_strike=row["long_strike"],
            width=row["width"],
            expiration=row["expiration"],
            short_symbol=row["short_symbol"],
            long_symbol=row["long_symbol"],
            quantity=row["quantity"],
            credit_received=row["credit_received"],
            open_order_id=row["open_order_id"],
            open_status=row["open_status"],
            opened_at=row["opened_at"],
            close_order_id=row["close_order_id"],
            close_status=row["close_status"],
            closed_at=row["closed_at"],
            close_reason=row["close_reason"],
            debit_paid=row["debit_paid"],
            pnl=row["pnl"],
            extra=extra,
        )

    # ─────────────────────── kill switch ─────────────────────────────────

    def get_kill_switch(self) -> KillSwitchState:
        row = self.conn.execute(
            "SELECT active, reason, triggered_at FROM kill_switch WHERE id = 1"
        ).fetchone()
        if row is None:
            return KillSwitchState(active=False, reason=None, triggered_at=None)
        return KillSwitchState(
            active=bool(row["active"]),
            reason=row["reason"],
            triggered_at=row["triggered_at"],
        )

    def trigger_kill_switch(self, reason: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            "UPDATE kill_switch SET active = 1, reason = ?, triggered_at = ? WHERE id = 1",
            (reason, now),
        )
        self.conn.commit()
        log.error("KILL SWITCH triggered: %s", reason)

    def reset_kill_switch(self) -> None:
        self.conn.execute(
            "UPDATE kill_switch SET active = 0, reason = NULL, triggered_at = NULL WHERE id = 1"
        )
        self.conn.commit()
        log.warning("Kill switch manually reset")

    # ─────────────────────── consecutive losses ──────────────────────────

    def consecutive_losses(self) -> int:
        """Count of consecutive losing trades from the end of the closed-trades log.
        A trade is a 'loss' if pnl < 0. Streak resets at the first non-loss."""
        rows = self.conn.execute(
            """
            SELECT pnl FROM trades
            WHERE close_status IS NOT NULL AND pnl IS NOT NULL
            ORDER BY closed_at DESC
            """
        ).fetchall()
        streak = 0
        for r in rows:
            if r["pnl"] is not None and r["pnl"] < 0:
                streak += 1
            else:
                break
        return streak

    # ─────────────────────── daily P&L ───────────────────────────────────

    def realized_pnl_on(self, on: date) -> float:
        row = self.conn.execute(
            """
            SELECT COALESCE(SUM(pnl), 0) AS total FROM trades
            WHERE date(closed_at) = ? AND pnl IS NOT NULL
            """,
            (on.isoformat(),),
        ).fetchone()
        return float(row["total"] or 0.0)

    def upsert_daily_pnl(
        self, on: date, *, starting_equity: Optional[float] = None,
        ending_equity: Optional[float] = None,
    ) -> None:
        closed = self.trades_closed_on(on)
        realized = sum(t.pnl or 0 for t in closed)
        wins = sum(1 for t in closed if (t.pnl or 0) > 0)
        losses = sum(1 for t in closed if (t.pnl or 0) < 0)
        self.conn.execute(
            """
            INSERT INTO daily_pnl(date, realized_pnl, trades, wins, losses, starting_equity, ending_equity)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(date) DO UPDATE SET
                realized_pnl = excluded.realized_pnl,
                trades = excluded.trades,
                wins = excluded.wins,
                losses = excluded.losses,
                starting_equity = COALESCE(excluded.starting_equity, daily_pnl.starting_equity),
                ending_equity   = COALESCE(excluded.ending_equity,   daily_pnl.ending_equity)
            """,
            (on.isoformat(), realized, len(closed), wins, losses, starting_equity, ending_equity),
        )
        self.conn.commit()

    # ─────────────────────── meta ────────────────────────────────────────

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            """
            INSERT INTO engine_meta(key, value, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (key, value, datetime.now(timezone.utc).isoformat()),
        )
        self.conn.commit()

    def get_meta(self, key: str, default: Optional[str] = None) -> Optional[str]:
        row = self.conn.execute(
            "SELECT value FROM engine_meta WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else default
