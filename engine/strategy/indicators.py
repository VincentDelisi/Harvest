"""Indicator math — SMA and RSI(2). Pure functions, no I/O.

All indicators are computed locally from raw OHLCV data — never trust
vendor-precomputed values. Different vendors use different RSI smoothing
methods; we use Wilder's classical RSI.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def sma(series: pd.Series, period: int) -> pd.Series:
    """Simple moving average. NaN for first (period-1) values."""
    if period <= 0:
        raise ValueError("period must be positive")
    return series.rolling(window=period, min_periods=period).mean()


def rsi_wilder(close: pd.Series, period: int = 2) -> pd.Series:
    """Wilder's RSI. Period=2 is intentional for short-term mean reversion (Connors RSI(2))."""
    if period <= 0:
        raise ValueError("period must be positive")
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)

    # Wilder's smoothing == EMA with alpha = 1/period
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    # When avg_loss == 0, RSI is 100 by convention
    rsi = rsi.where(avg_loss != 0, 100.0)
    return rsi


def latest_value(series: pd.Series) -> float | None:
    """Last non-NaN value, or None."""
    if series.empty:
        return None
    s = series.dropna()
    return float(s.iloc[-1]) if not s.empty else None
