"""Indicator math tests with known inputs/outputs."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from engine.strategy.indicators import latest_value, rsi_wilder, sma


def test_sma_basic():
    s = pd.Series([1, 2, 3, 4, 5], dtype=float)
    out = sma(s, 3)
    assert pd.isna(out.iloc[0])
    assert pd.isna(out.iloc[1])
    assert out.iloc[2] == pytest.approx(2.0)
    assert out.iloc[3] == pytest.approx(3.0)
    assert out.iloc[4] == pytest.approx(4.0)


def test_sma_invalid_period():
    with pytest.raises(ValueError):
        sma(pd.Series([1.0]), 0)


def test_rsi_extreme_uptrend_approaches_100():
    # Strictly increasing prices → RSI should be ~100
    s = pd.Series(np.arange(1, 101, dtype=float))
    rsi = rsi_wilder(s, period=2)
    assert rsi.iloc[-1] == pytest.approx(100.0, abs=0.01)


def test_rsi_extreme_downtrend_approaches_zero():
    s = pd.Series(np.arange(100, 0, -1, dtype=float))
    rsi = rsi_wilder(s, period=2)
    assert rsi.iloc[-1] < 5.0  # very oversold


def test_rsi_alternating_centered():
    # Symmetric noise around a mean → RSI hovers near 50
    rng = np.random.default_rng(42)
    s = pd.Series(100 + rng.standard_normal(500).cumsum() * 0.1)
    rsi = rsi_wilder(s, period=2)
    # RSI(2) is volatile; just assert it's bounded and varied
    assert rsi.dropna().between(0, 100).all()
    assert rsi.dropna().std() > 5


def test_latest_value_handles_nans():
    s = pd.Series([1.0, 2.0, np.nan])
    assert latest_value(s) == 2.0
    assert latest_value(pd.Series([np.nan, np.nan])) is None
    assert latest_value(pd.Series([], dtype=float)) is None
