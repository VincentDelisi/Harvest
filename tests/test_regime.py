"""Regime classification tests."""
from __future__ import annotations

import numpy as np
import pandas as pd

from engine.strategy.regime import Regime, classify


def _make_bars(closes: list[float]) -> pd.DataFrame:
    idx = pd.date_range("2025-01-01", periods=len(closes), freq="B")
    return pd.DataFrame({"close": closes}, index=idx)


def test_bull_regime():
    # 250 days of monotonically increasing prices → close > sma50 > sma200
    closes = list(np.linspace(100, 200, 250))
    snap = classify("SPY", _make_bars(closes))
    assert snap.regime == Regime.BULL
    assert snap.trades_puts is True
    assert snap.trades_calls is False
    assert snap.close > snap.sma50 > snap.sma200


def test_bear_regime():
    closes = list(np.linspace(200, 100, 250))
    snap = classify("SPY", _make_bars(closes))
    assert snap.regime == Regime.BEAR
    assert snap.trades_calls is True
    assert snap.trades_puts is False


def test_mixed_regime_short_history():
    # Only 100 bars → not enough for SMA200 → MIXED
    closes = list(np.linspace(100, 150, 100))
    snap = classify("SPY", _make_bars(closes))
    assert snap.regime == Regime.MIXED


def test_mixed_regime_when_close_below_sma50_but_above_sma200():
    # Construct a series where close < sma50 but close > sma200 (transition).
    # Long uptrend, then recent pullback — typical 'MIXED' setup the engine should sit out.
    up = list(np.linspace(100, 200, 200))
    pullback = list(np.linspace(200, 180, 50))
    closes = up + pullback
    snap = classify("SPY", _make_bars(closes))
    # close (180) < sma50 (~190) AND sma50 (~190) > sma200 (~150) → not BULL, not BEAR → MIXED
    assert snap.close < snap.sma50
    assert snap.sma50 > snap.sma200
    assert snap.regime == Regime.MIXED
