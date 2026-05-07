"""Yahoo Finance fallback for index quotes (VIX, VXN, RVX).

Used when Polygon plan does not include indices. Yahoo's chart endpoint is
free, requires no API key, and returns daily OHLC for indices that we use
to bootstrap IVR/IVP and feed the kill switch.

Symbol mapping:
  - VIX → ^VIX
  - VXN → ^VXN
  - RVX → ^RVX
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Iterable

import httpx
import pandas as pd
from tenacity import retry, stop_after_attempt, wait_exponential

from engine.utils.logging import get_logger

log = get_logger(__name__)

# Yahoo's chart endpoint. Public, unauthenticated, but it expects a real-ish
# User-Agent or it returns 401.
_BASE = "https://query1.finance.yahoo.com"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

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

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client or httpx.Client(
            base_url=_BASE, headers=_HEADERS, timeout=15.0
        )

    def close(self) -> None:
        self._client.close()

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
    def daily_bars(self, ticker: str, lookback_days: int = 300) -> pd.DataFrame:
        """Daily OHLC for an index. Returns DataFrame indexed by ET date."""
        symbol = _yahoo_symbol(ticker)
        end_dt = datetime.now(tz=timezone.utc)
        start_dt = end_dt - timedelta(days=lookback_days * 2)
        params = {
            "period1": int(start_dt.timestamp()),
            "period2": int(end_dt.timestamp()),
            "interval": "1d",
            "includePrePost": "false",
            "events": "div,splits",
        }
        resp = self._client.get(f"/v8/finance/chart/{symbol}", params=params)
        resp.raise_for_status()
        payload = resp.json()
        result = (payload.get("chart") or {}).get("result") or []
        if not result:
            log.warning("Yahoo returned no data for %s", symbol)
            return pd.DataFrame(
                columns=["open", "high", "low", "close", "volume"]
            )
        bar = result[0]
        timestamps: Iterable[int] = bar.get("timestamp") or []
        indicators = (bar.get("indicators") or {}).get("quote") or [{}]
        quote = indicators[0]
        if not timestamps:
            return pd.DataFrame(
                columns=["open", "high", "low", "close", "volume"]
            )
        df = pd.DataFrame(
            {
                "open": quote.get("open") or [],
                "high": quote.get("high") or [],
                "low": quote.get("low") or [],
                "close": quote.get("close") or [],
                "volume": quote.get("volume") or [],
            }
        )
        df["timestamp"] = pd.to_datetime(list(timestamps), unit="s", utc=True)
        df = df.set_index("timestamp").dropna(subset=["close"])
        return df

    def latest_value(self, ticker: str) -> float | None:
        """Latest close for an index (yesterday's settle, since indices update EOD)."""
        df = self.daily_bars(ticker, lookback_days=10)
        if df.empty:
            log.warning("Yahoo: no data for index %s", ticker)
            return None
        return float(df["close"].iloc[-1])
