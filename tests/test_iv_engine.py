"""IVR/IVP engine tests."""
from __future__ import annotations

import tempfile
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

from engine.strategy.iv_engine import IVEngine


@pytest.fixture
def tmp_engine():
    with tempfile.TemporaryDirectory() as tmp:
        eng = IVEngine(db_path=str(Path(tmp) / "test.db"))
        yield eng
        eng.close()


def test_log_and_history_roundtrip(tmp_engine):
    base = date(2025, 1, 1)
    for i in range(10):
        tmp_engine.log_atm_iv("SPY", base + timedelta(days=i), 0.15 + i * 0.001)
    df = tmp_engine.history("SPY")
    assert len(df) == 10
    assert df["atm_iv"].is_monotonic_increasing


def test_ivr_at_min_max_bounds(tmp_engine):
    # 252 entries spanning IV range [0.10, 0.30], ending today so they
    # fall inside the engine's 504-day lookback window.
    base = date.today() - timedelta(days=251)
    for i in range(252):
        iv = 0.10 + (i / 251) * 0.20
        tmp_engine.log_atm_iv("SPY", base + timedelta(days=i), iv)

    # Today IV at min → IVR ≈ 0
    stats_low = tmp_engine.compute_stats("SPY", today_iv=0.10)
    assert stats_low.ivr == pytest.approx(0.0, abs=0.5)

    # Today IV at max → IVR ≈ 100
    stats_high = tmp_engine.compute_stats("SPY", today_iv=0.30)
    assert stats_high.ivr == pytest.approx(100.0, abs=0.5)

    # Mid → IVR ≈ 50
    stats_mid = tmp_engine.compute_stats("SPY", today_iv=0.20)
    assert 45 <= stats_mid.ivr <= 55
    assert stats_mid.bootstrap is False


def test_bootstrap_used_when_history_short(tmp_engine):
    # Only 5 days of own history → bootstrap engaged
    base = date.today() - timedelta(days=4)
    for i in range(5):
        tmp_engine.log_atm_iv("SPY", base + timedelta(days=i), 0.15)
    bootstrap_series = pd.Series([12.0, 14.0, 16.0, 18.0, 20.0] * 20)  # 100 entries
    stats = tmp_engine.compute_stats("SPY", today_iv=18.0, bootstrap_history=bootstrap_series)
    assert stats.bootstrap is True
    assert stats.sample_size == len(bootstrap_series)
    assert 0 <= stats.ivr <= 100


def test_passes_volatility_gate():
    from engine.strategy.iv_engine import IVStats
    from engine.utils.config import CONFIG

    # IVR exactly at threshold → pass
    s = IVStats("SPY", 0.20, ivr=CONFIG.ivr_min, ivp=0.0, bootstrap=False, sample_size=252)
    assert IVEngine.passes_volatility_gate(s) is True

    # Both below → fail
    s2 = IVStats("SPY", 0.20, ivr=10.0, ivp=10.0, bootstrap=False, sample_size=252)
    assert IVEngine.passes_volatility_gate(s2) is False

    # Low IVR but high IVP → pass (the OR clause)
    s3 = IVStats("SPY", 0.20, ivr=5.0, ivp=40.0, bootstrap=False, sample_size=252)
    assert IVEngine.passes_volatility_gate(s3) is True
