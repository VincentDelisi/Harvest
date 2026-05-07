"""IV Rank / IV Percentile engine per STRATEGY_SPEC.md §6.

Stores daily ATM IV per underlying in SQLite. Computes IVR/IVP from the
trailing 252 trading days. Bootstraps from VIX/VXN/RVX until the local
history is long enough.

Today's ATM IV is sourced from Public's option chain at ~30 DTE (logged
once per day at 15:55 ET by the engine main loop).
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from engine.utils.config import CONFIG
from engine.utils.logging import get_logger

log = get_logger(__name__)


@dataclass
class IVStats:
    symbol: str
    today_iv: float
    ivr: float        # 0–100
    ivp: float        # 0–100
    bootstrap: bool   # True if computed from VIX/VXN/RVX rather than own history
    sample_size: int


class IVEngine:
    """Persists daily ATM IV per symbol; computes IVR & IVP."""

    def __init__(self, db_path: str | None = None) -> None:
        path = Path(db_path or CONFIG.sqlite_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS iv_history (
                symbol TEXT NOT NULL,
                date   TEXT NOT NULL,
                atm_iv REAL NOT NULL,
                PRIMARY KEY (symbol, date)
            )
            """
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def log_atm_iv(self, symbol: str, on: date, atm_iv: float) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO iv_history(symbol, date, atm_iv) VALUES (?,?,?)",
            (symbol, on.isoformat(), float(atm_iv)),
        )
        self.conn.commit()

    def history(self, symbol: str, lookback_days: int = 252) -> pd.DataFrame:
        cutoff = (date.today() - timedelta(days=lookback_days * 2)).isoformat()
        rows = self.conn.execute(
            "SELECT date, atm_iv FROM iv_history WHERE symbol=? AND date>=? ORDER BY date ASC",
            (symbol, cutoff),
        ).fetchall()
        df = pd.DataFrame(rows, columns=["date", "atm_iv"])
        if df.empty:
            return df
        df["date"] = pd.to_datetime(df["date"])
        return df.set_index("date").tail(lookback_days)

    def compute_stats(
        self,
        symbol: str,
        today_iv: float,
        bootstrap_history: pd.Series | None = None,
    ) -> IVStats:
        """Compute IVR and IVP.

        Uses local SQLite history if ≥252 entries exist; otherwise falls back
        to provided bootstrap_history (typically VIX/VXN/RVX daily closes).
        """
        own = self.history(symbol)
        if len(own) >= 252:
            series = own["atm_iv"]
            bootstrap = False
        elif bootstrap_history is not None and len(bootstrap_history) >= 60:
            series = bootstrap_history
            bootstrap = True
            log.info(
                "IVR/IVP for %s using bootstrap series (n=%d, own history=%d)",
                symbol,
                len(series),
                len(own),
            )
        else:
            log.warning(
                "Insufficient data for IVR/IVP on %s (own=%d, bootstrap=%s)",
                symbol,
                len(own),
                "none" if bootstrap_history is None else len(bootstrap_history),
            )
            return IVStats(symbol, today_iv, ivr=0.0, ivp=0.0, bootstrap=True, sample_size=0)

        s = series.astype(float)
        lo, hi = float(s.min()), float(s.max())
        ivr = 0.0 if hi == lo else (today_iv - lo) / (hi - lo) * 100.0
        ivr = max(0.0, min(100.0, ivr))
        ivp = float((s < today_iv).sum()) / len(s) * 100.0

        return IVStats(
            symbol=symbol,
            today_iv=today_iv,
            ivr=ivr,
            ivp=ivp,
            bootstrap=bootstrap,
            sample_size=len(s),
        )

    @staticmethod
    def passes_volatility_gate(stats: IVStats) -> bool:
        """STRATEGY_SPEC.md §4.2: IVR ≥ 20 OR IVP ≥ 30."""
        return stats.ivr >= CONFIG.ivr_min or stats.ivp >= CONFIG.ivp_min
