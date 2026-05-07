"""Polygon REST client — daily/intraday aggregates and snapshot quotes.

Used for:
  - Daily SMA(50) and SMA(200) for regime detection (§3)
  - Intraday 5-min bars for RSI(2) trigger (§4.4)
  - VIX/VXN/RVX historical values for IVR/IVP bootstrap (§6.4)

WebSocket is preferred for live ticks — see polygon_ws.py.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Literal

import httpx
import pandas as pd
from tenacity import retry, stop_after_attempt, wait_exponential

from engine.data.yahoo_indices import YahooIndices
from engine.utils.config import CONFIG
from engine.utils.logging import get_logger

log = get_logger(__name__)

Timespan = Literal["minute", "hour", "day", "week", "month"]


class PolygonREST:
    def __init__(self, yahoo_fallback: YahooIndices | None = None) -> None:
        if not CONFIG.polygon_api_key:
            raise RuntimeError("POLYGON_API_KEY is not set in .env")
        self._client = httpx.Client(
            base_url=CONFIG.polygon_rest_base,
            params={"apiKey": CONFIG.polygon_api_key},
            timeout=15.0,
        )
        # Yahoo fallback for indices when Polygon plan doesn't include them.
        self._yahoo = yahoo_fallback if yahoo_fallback is not None else YahooIndices()

    def close(self) -> None:
        self._client.close()
        try:
            self._yahoo.close()
        except Exception:  # pragma: no cover
            pass

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
    def aggregates(
        self,
        ticker: str,
        multiplier: int,
        timespan: Timespan,
        start: date,
        end: date,
        adjusted: bool = True,
        limit: int = 50_000,
    ) -> pd.DataFrame:
        """Fetch OHLCV aggregates. Returns DataFrame indexed by ET timestamp.

        Ticker conventions:
          - Equity/ETF: "SPY", "QQQ", "IWM"
          - Index: "I:VIX", "I:VXN", "I:RVX" (Polygon prefixes indices with "I:")
        """
        url = f"/v2/aggs/ticker/{ticker}/range/{multiplier}/{timespan}/{start.isoformat()}/{end.isoformat()}"
        resp = self._client.get(
            url,
            params={"adjusted": str(adjusted).lower(), "sort": "asc", "limit": limit},
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") not in ("OK", "DELAYED"):
            log.warning("Polygon aggregates non-OK status: %s", data.get("status"))
        results = data.get("results", []) or []
        if not results:
            return pd.DataFrame(
                columns=["open", "high", "low", "close", "volume", "vwap", "trades"]
            )
        df = pd.DataFrame(results)
        df["timestamp"] = pd.to_datetime(df["t"], unit="ms", utc=True).dt.tz_convert(
            CONFIG.timezone
        )
        df = df.rename(
            columns={
                "o": "open",
                "h": "high",
                "l": "low",
                "c": "close",
                "v": "volume",
                "vw": "vwap",
                "n": "trades",
            }
        ).set_index("timestamp")
        return df[["open", "high", "low", "close", "volume", "vwap", "trades"]]

    def daily_bars(self, ticker: str, lookback_days: int = 300) -> pd.DataFrame:
        """Convenience wrapper — daily bars for SMA/regime computation."""
        end = date.today()
        start = end - timedelta(days=lookback_days * 2)  # account for weekends/holidays
        return self.aggregates(ticker, 1, "day", start, end)

    def intraday_5m(self, ticker: str, lookback_minutes: int = 240) -> pd.DataFrame:
        """5-minute bars for the current session — used for RSI(2) trigger."""
        now = datetime.now()
        start = (now - timedelta(minutes=lookback_minutes)).date()
        end = now.date()
        df = self.aggregates(ticker, 5, "minute", start, end)
        # Filter to today's regular trading hours
        if not df.empty:
            today = pd.Timestamp.now(tz=CONFIG.timezone).normalize()
            df = df[df.index >= today]
        return df

    def latest_index_value(self, index_ticker: str) -> float | None:
        """Latest VIX/VXN/RVX value (most recent close).

        Routes to Yahoo Finance when POLYGON_HAS_INDICES is false (default), since
        Polygon's Stocks plans don't include indices.
        """
        if not CONFIG.polygon_has_indices:
            return self._yahoo.latest_value(index_ticker)
        ticker = index_ticker if index_ticker.startswith("I:") else f"I:{index_ticker}"
        df = self.daily_bars(ticker, lookback_days=10)
        if df.empty:
            log.warning("No data for index %s", ticker)
            return None
        return float(df["close"].iloc[-1])

    def index_daily_bars(self, index_ticker: str, lookback_days: int = 300) -> pd.DataFrame:
        """Daily bars for an index, used for IVR/IVP bootstrap.

        Routes to Yahoo Finance when POLYGON_HAS_INDICES is false.
        """
        if not CONFIG.polygon_has_indices:
            return self._yahoo.daily_bars(index_ticker, lookback_days=lookback_days)
        ticker = index_ticker if index_ticker.startswith("I:") else f"I:{index_ticker}"
        return self.daily_bars(ticker, lookback_days=lookback_days)
