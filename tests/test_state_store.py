"""StateStore — trade lifecycle, kill switch, P&L, consecutive losses."""
from __future__ import annotations

from datetime import date

import pytest

from engine.state.store import StateStore, TradeRecord


@pytest.fixture
def store(tmp_path):
    s = StateStore(db_path=str(tmp_path / "test.db"))
    yield s
    s.close()


def _trade(trade_id: str, **overrides) -> TradeRecord:
    base = TradeRecord(
        trade_id=trade_id,
        underlying="SPY",
        direction="PUT",
        short_strike=580.0,
        long_strike=579.0,
        width=1.0,
        expiration="2026-05-08",
        short_symbol="SPY260508P00580000",
        long_symbol="SPY260508P00579000",
        quantity=1,
        credit_received=0.40,
        opened_at="2026-05-07T10:15:00-04:00",
        open_status="FILLED",
    )
    for k, v in overrides.items():
        setattr(base, k, v)
    return base


def test_insert_and_get_trade(store):
    store.insert_trade(_trade("t1"))
    got = store.get_trade("t1")
    assert got is not None
    assert got.underlying == "SPY"
    assert got.short_strike == 580.0


def test_open_trades_filters_correctly(store):
    store.insert_trade(_trade("t1", open_status="FILLED"))
    store.insert_trade(_trade("t2", open_status="PENDING"))
    store.insert_trade(_trade("t3", open_status="FILLED", close_status="FILLED", pnl=20.0))
    opens = store.open_trades()
    assert {t.trade_id for t in opens} == {"t1"}


def test_record_close_computes_pnl(store):
    # Credit 0.40, close debit 0.20 → P&L per spread = 0.20 → $20 × qty
    store.insert_trade(_trade("t1", quantity=2, credit_received=0.40))
    updated = store.record_close(
        "t1",
        close_order_id="c1",
        close_status="FILLED",
        closed_at="2026-05-08T10:00:00-04:00",
        close_reason="PROFIT_TARGET",
        debit_paid=0.20,
    )
    assert updated is not None
    assert updated.pnl == 40.0  # (0.40 - 0.20) * 100 * 2
    assert updated.close_reason == "PROFIT_TARGET"


def test_record_close_loss(store):
    # Credit 0.40, close debit 1.20 (max loss minus a hair) → -0.80 × 100 = -80
    store.insert_trade(_trade("t1"))
    updated = store.record_close(
        "t1",
        close_order_id="c1",
        close_status="FILLED",
        closed_at="2026-05-08T15:25:00-04:00",
        close_reason="STOP_LOSS",
        debit_paid=1.20,
    )
    assert updated.pnl == -80.0


def test_consecutive_losses_streak(store):
    store.insert_trade(_trade("a"))
    store.record_close("a", close_order_id=None, close_status="FILLED",
                       closed_at="2026-05-01T16:00:00-04:00",
                       close_reason="STOP_LOSS", debit_paid=1.20)
    store.insert_trade(_trade("b"))
    store.record_close("b", close_order_id=None, close_status="FILLED",
                       closed_at="2026-05-02T16:00:00-04:00",
                       close_reason="STOP_LOSS", debit_paid=1.20)
    store.insert_trade(_trade("c"))
    store.record_close("c", close_order_id=None, close_status="FILLED",
                       closed_at="2026-05-03T16:00:00-04:00",
                       close_reason="STOP_LOSS", debit_paid=1.20)
    assert store.consecutive_losses() == 3

    # A win resets the streak
    store.insert_trade(_trade("d"))
    store.record_close("d", close_order_id=None, close_status="FILLED",
                       closed_at="2026-05-04T16:00:00-04:00",
                       close_reason="PROFIT_TARGET", debit_paid=0.20)
    assert store.consecutive_losses() == 0


def test_kill_switch_lifecycle(store):
    assert store.get_kill_switch().active is False
    store.trigger_kill_switch("test reason")
    ks = store.get_kill_switch()
    assert ks.active is True
    assert ks.reason == "test reason"
    store.reset_kill_switch()
    assert store.get_kill_switch().active is False


def test_realized_pnl_on_date(store):
    store.insert_trade(_trade("a"))
    store.record_close("a", close_order_id=None, close_status="FILLED",
                       closed_at="2026-05-07T12:00:00-04:00",
                       close_reason="PROFIT_TARGET", debit_paid=0.20)
    store.insert_trade(_trade("b"))
    store.record_close("b", close_order_id=None, close_status="FILLED",
                       closed_at="2026-05-07T15:00:00-04:00",
                       close_reason="PROFIT_TARGET", debit_paid=0.20)
    pnl = store.realized_pnl_on(date(2026, 5, 7))
    assert pnl == 40.0  # 2 × $20


def test_meta_kv(store):
    assert store.get_meta("k", default="x") == "x"
    store.set_meta("k", "v1")
    assert store.get_meta("k") == "v1"
    store.set_meta("k", "v2")
    assert store.get_meta("k") == "v2"
