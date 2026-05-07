"""Position sizing — risk fractions, mode caps, max-loss math."""
from __future__ import annotations

from engine.runtime.sizing import (
    aggregate_max_loss_pct,
    per_spread_max_loss,
    size_position,
)


def test_per_spread_max_loss_basic():
    # $1 width, $0.40 credit → max loss = $0.60 × 100 = $60
    assert per_spread_max_loss(1.0, 0.40) == 60.0


def test_per_spread_max_loss_floor_when_full_credit():
    # If credit equals width (impossible in real life, but defensive), still positive
    assert per_spread_max_loss(1.0, 1.0) == 0.01


def test_dry_run_returns_one():
    qty = size_position(
        account_equity=100_000, width=1.0, credit=0.40,
        mode="DRY_RUN", filled_trade_count=0,
    )
    assert qty == 1


def test_live_small_caps_at_one():
    qty = size_position(
        account_equity=1_000_000, width=1.0, credit=0.40,
        mode="LIVE_SMALL", filled_trade_count=0,
    )
    assert qty == 1


def test_live_first_30_trades_use_initial_risk_capped_at_one():
    # 0.5% of 100k = $500 / $60 max loss per spread = 8 contracts theoretical
    # But first 30 trades cap = 1
    qty = size_position(
        account_equity=100_000, width=1.0, credit=0.40,
        mode="LIVE", filled_trade_count=29,
    )
    assert qty == 1


def test_live_after_30_trades_uses_full_risk():
    # 1% of 100k = $1000 / $60 = 16 contracts
    qty = size_position(
        account_equity=100_000, width=1.0, credit=0.40,
        mode="LIVE", filled_trade_count=30,
    )
    assert qty == 16


def test_zero_equity_returns_zero():
    qty = size_position(
        account_equity=0, width=1.0, credit=0.40,
        mode="LIVE", filled_trade_count=100,
    )
    assert qty == 0


def test_aggregate_max_loss_pct():
    # Two open positions each risking $60, plus a proposed $60 = $180 / $10000 = 1.8%
    assert aggregate_max_loss_pct([60, 60], 60, 10_000) == 0.018
