"""Daily regime classification per STRATEGY_SPEC.md §3.

Regime is computed once per trading day at 09:30 ET from the prior close.
Does not change intraday.

  BULL  if close > sma50  AND  sma50 > sma200  →  sell put credit spreads
  BEAR  if close < sma50  AND  sma50 < sma200  →  sell call credit spreads
  MIXED otherwise                              →  no trades
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import pandas as pd

from engine.strategy.indicators import latest_value, sma


class Regime(str, Enum):
    BULL = "BULL"
    BEAR = "BEAR"
    MIXED = "MIXED"


@dataclass
class RegimeSnapshot:
    symbol: str
    regime: Regime
    close: float
    sma50: float
    sma200: float
    as_of: pd.Timestamp

    @property
    def trades_puts(self) -> bool:
        return self.regime == Regime.BULL

    @property
    def trades_calls(self) -> bool:
        return self.regime == Regime.BEAR


def classify(symbol: str, daily_bars: pd.DataFrame) -> RegimeSnapshot:
    """Classify regime from daily OHLCV bars.

    Args:
        symbol: ticker, e.g. "SPY"
        daily_bars: DataFrame with 'close' column, indexed by date, sorted ascending.
                    Must contain ≥200 closes for SMA200 to be valid.
    """
    if "close" not in daily_bars.columns:
        raise ValueError("daily_bars must have 'close' column")
    if len(daily_bars) < 200:
        # Not enough history → MIXED is the safe default (no trades)
        return RegimeSnapshot(
            symbol=symbol,
            regime=Regime.MIXED,
            close=float(daily_bars["close"].iloc[-1]) if not daily_bars.empty else 0.0,
            sma50=float("nan"),
            sma200=float("nan"),
            as_of=daily_bars.index[-1] if not daily_bars.empty else pd.Timestamp.now(),
        )

    close = float(daily_bars["close"].iloc[-1])
    sma50_val = latest_value(sma(daily_bars["close"], 50)) or float("nan")
    sma200_val = latest_value(sma(daily_bars["close"], 200)) or float("nan")

    if close > sma50_val and sma50_val > sma200_val:
        regime = Regime.BULL
    elif close < sma50_val and sma50_val < sma200_val:
        regime = Regime.BEAR
    else:
        regime = Regime.MIXED

    return RegimeSnapshot(
        symbol=symbol,
        regime=regime,
        close=close,
        sma50=sma50_val,
        sma200=sma200_val,
        as_of=daily_bars.index[-1],
    )
