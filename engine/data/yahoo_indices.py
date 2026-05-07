"""Yahoo Finance fallback for index quotes (VIX, VXN, RVX).

Used when Polygon plan does not include indices. Backed by yfinance, which
handles cookies/crumbs, anti-bot mitigations, and retries.

Symbol mapping:
  - VIX → ^VIX
  - VXN → ^VXN
  - RVX → ^RVX
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import yfinance as yf
from tenacity import retry, stop_after_attempt, wait_exponential

from engine.utils.logging import get_logger

log = get_logger(__name__)

_SYMBOL_MAP = {
    "VIX": "^VIX",
    "VXN": "^VXN",
    "RVX": "^RVX",
    "I:VIX": "^VIX",
    "I:VXN": "^VXN",
    "I:RVX": "^RVX",
}


def _yahoo_symbol(ticker: str) -> str:
    return _SYMBOL_MAP.get(ticker.upper(), ticker)


class YahooIndices:
    """Free fallback for index data when Polygon Indices plan is unavailable."""

    def __init__(self, client: object | None = None) -> None:
        # `client` accepted for API compatibility but unused (yfinance manages its own session).
        self._client = client

    def close(self) -> None:
        pass

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
    def daily_bars(self, ticker: str, lookback_days: int = 300) -> pd.DataFrame:
        """Daily OHLC for an index. Returns DataFrame with [open, high, low, close, volume]."""
        symbol = _yahoo_symbol(ticker)
        end_dt = datetime.now(tz=timezone.utc)
        start_dt = end_dt - timedelta(days=lookback_days * 2)
        try:
            tkr = yf.Ticker(symbol)
            raw = tkr.history(
                start=start_dt.date().isoformat(),
                end=end_dt.date().isoformat(),
                interval="1d",
                auto_adjust=False,
                actions=False,
            )
        except Exception as exc:
            log.warning("yfinance failed for %s: %s", symbol, exc)
            raise

        if raw is None or raw.empty:
            log.warning("Yahoo returned no data for %s", symbol)
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

        df = raw.rename(
            columns={
                "Open": "open",
                "High": "high",
                "Low": "low",
                "Close": "close",
                "Volume": "volume",
            }
        )[["open", "high", "low", "close", "volume"]]
        df = df.dropna(subset=["close"])
        # Normalize index to UTC tz-aware to match Polygon convention
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC")
        else:
            df.index = df.index.tz_convert("UTC")
        return df

    def latest_value(self, ticker: str) -> float | None:
        """Latest close for an index."""
        df = self.daily_bars(ticker, lookback_days=10)
        if df.empty:
            log.warning("Yahoo: no data for index %s", ticker)
            return None
        return float(df["close"].iloc[-1])
