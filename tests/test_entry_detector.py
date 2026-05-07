"""Entry detector — gates, RSI trigger, sizing/concurrency, order placement."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import httpx
import numpy as np
import pandas as pd
import pytest
import pytz

from engine.broker.auth import TokenManager
from engine.broker.public_client import PublicClient
from engine.notify.discord import NullNotifier
from engine.runtime.entry_detector import EntryDetector
from engine.runtime.market_context import MarketContext, UnderlyingContext
from engine.state.store import StateStore
from engine.strategy.iv_engine import IVStats
from engine.strategy.regime import Regime, RegimeSnapshot

ET = pytz.timezone("America/New_York")


# ─────────────── helpers ───────────────────────────────────────────

def _stub_token_manager() -> TokenManager:
    tm = TokenManager(secret="fake")
    tm._cached = type("T", (), {"token": "tok", "expires_at_epoch": 1e18})()
    return tm


def _bull_ctx_for(symbol: str = "SPY") -> UnderlyingContext:
    return UnderlyingContext(
        symbol=symbol,
        regime=RegimeSnapshot(
            symbol=symbol, regime=Regime.BULL,
            close=580.0, sma50=570.0, sma200=550.0,
            as_of=pd.Timestamp("2026-05-07"),
        ),
        iv=IVStats(symbol=symbol, today_iv=18.0, ivr=25.0, ivp=40.0,
                   bootstrap=True, sample_size=252),
        vix_proxy_value=15.0,
        iv_gate_passed=True,
    )


def _market_ctx(blackout=False, vix=15.0, iv_passed=True) -> MarketContext:
    u = _bull_ctx_for("SPY")
    u.iv_gate_passed = iv_passed
    return MarketContext(
        as_of=datetime(2026, 5, 7, 10, 30, tzinfo=ET),
        blackout_active=blackout,
        blackout_reason="FOMC" if blackout else None,
        vix_value=vix,
        vix_gate_passed=(vix < 30),
        underlyings={"SPY": u},
    )


def _rsi_low_bars() -> pd.DataFrame:
    """5-min bars with sharp recent dip → RSI(2) crashes below 10."""
    base = np.array([100.0] * 20 + [99.5, 99.0, 98.0, 96.0, 94.0])
    idx = pd.date_range("2026-05-07 10:00", periods=len(base), freq="5min", tz=ET)
    return pd.DataFrame({"close": base}, index=idx)


def _rsi_neutral_bars() -> pd.DataFrame:
    base = np.linspace(99.0, 101.0, 25)
    idx = pd.date_range("2026-05-07 10:00", periods=len(base), freq="5min", tz=ET)
    return pd.DataFrame({"close": base}, index=idx)


def _make_public_client(handler):
    return PublicClient(
        account_id="acct-1",
        base_url="https://api.public.com",
        token_manager=_stub_token_manager(),
        dry_run=False,
        transport=httpx.MockTransport(handler),
    )


# ─────────────── tests ─────────────────────────────────────────────

@pytest.fixture
def state(tmp_path):
    s = StateStore(db_path=str(tmp_path / "e.db"))
    yield s
    s.close()


def test_blocks_when_blackout_active(state):
    public = _make_public_client(lambda r: httpx.Response(500))
    ed = EntryDetector(public, state, NullNotifier())
    decision = ed.check_underlying(
        "SPY", _rsi_low_bars(), _market_ctx(blackout=True),
        account_equity=10_000, now_et=datetime(2026, 5, 7, 10, 30, tzinfo=ET),
    )
    assert decision.placed_order_id is None
    assert decision.triggered is False
    assert decision.blocked_reason and "blackout" in decision.blocked_reason.lower()


def test_blocks_when_regime_mixed(state):
    public = _make_public_client(lambda r: httpx.Response(500))
    ed = EntryDetector(public, state, NullNotifier())
    ctx = _market_ctx()
    ctx.underlyings["SPY"].regime = RegimeSnapshot(
        symbol="SPY", regime=Regime.MIXED, close=580, sma50=570, sma200=575,
        as_of=pd.Timestamp("2026-05-07"),
    )
    d = ed.check_underlying(
        "SPY", _rsi_low_bars(), ctx, 10_000,
        datetime(2026, 5, 7, 10, 30, tzinfo=ET),
    )
    assert d.placed_order_id is None
    assert d.blocked_reason == "regime MIXED"


def test_no_trigger_when_rsi_neutral(state):
    public = _make_public_client(lambda r: httpx.Response(500))
    ed = EntryDetector(public, state, NullNotifier())
    d = ed.check_underlying(
        "SPY", _rsi_neutral_bars(), _market_ctx(), 10_000,
        datetime(2026, 5, 7, 10, 30, tzinfo=ET),
    )
    assert d.triggered is False
    assert d.placed_order_id is None


def test_full_path_places_order(state):
    """RSI crashes → expirations OK → chain returns valid spread → preflight OK → order placed."""
    calls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        calls.append(req.method + " " + path)

        if path.endswith("/option-expirations"):
            return httpx.Response(200, json={
                "baseSymbol": "SPY",
                "expirations": ["2026-05-09"],  # 2 DTE from 2026-05-07
            })

        if path.endswith("/option-chain"):
            puts = [
                {"instrument": {"symbol": "SPY260509P00580000", "type": "OPTION"},
                 "outcome": "SUCCESS", "bid": "0.40", "ask": "0.41",
                 "openInterest": "1500", "delta": "-0.20", "strikePrice": "580.00"},
                {"instrument": {"symbol": "SPY260509P00579000", "type": "OPTION"},
                 "outcome": "SUCCESS", "bid": "0.04", "ask": "0.05",
                 "openInterest": "1500", "delta": "-0.10", "strikePrice": "579.00"},
            ]
            return httpx.Response(200, json={
                "baseSymbol": "SPY", "calls": [], "puts": puts,
            })

        if path.endswith("/preflight/multi-leg"):
            return httpx.Response(200, json={
                "baseSymbol": "SPY", "strategyName": "VERTICAL_PUT_CREDIT",
                "legs": [], "orderValue": "60.00",
                "buyingPowerRequirement": "60.00",
            })

        if path.endswith("/order/multileg"):
            body = json.loads(req.content)
            return httpx.Response(200, json={"orderId": body["orderId"]})

        return httpx.Response(404)

    public = _make_public_client(handler)
    ed = EntryDetector(public, state, NullNotifier())
    d = ed.check_underlying(
        "SPY", _rsi_low_bars(), _market_ctx(),
        account_equity=10_000,
        now_et=datetime(2026, 5, 7, 10, 30, tzinfo=ET),
    )
    assert d.triggered is True
    assert d.candidate is not None
    assert d.placed_order_id is not None
    # Preflight must have been called BEFORE the order
    pre_idx = next(i for i, c in enumerate(calls) if "preflight" in c)
    ord_idx = next(i for i, c in enumerate(calls) if "order/multileg" in c)
    assert pre_idx < ord_idx
    # Trade was persisted PENDING
    pending = state.pending_trades()
    assert len(pending) == 1
    assert pending[0].underlying == "SPY"
    assert pending[0].direction == "PUT"


def test_concurrency_cap_per_underlying(state):
    """If 2 SPY positions are already open, no new SPY entry is allowed."""
    from engine.state.store import TradeRecord
    for tid in ("a", "b"):
        state.insert_trade(TradeRecord(
            trade_id=tid, underlying="SPY", direction="PUT",
            short_strike=580, long_strike=579, width=1.0,
            expiration="2026-05-09",
            short_symbol="X", long_symbol="Y",
            quantity=1, credit_received=0.40,
            opened_at="2026-05-07T10:15:00-04:00", open_status="FILLED",
        ))

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path.endswith("/option-expirations"):
            return httpx.Response(200, json={"baseSymbol": "SPY", "expirations": ["2026-05-09"]})
        if path.endswith("/option-chain"):
            return httpx.Response(200, json={"baseSymbol": "SPY", "calls": [], "puts": [
                {"instrument": {"symbol": "SPY260509P00580000", "type": "OPTION"},
                 "outcome": "SUCCESS", "bid": "0.40", "ask": "0.41",
                 "openInterest": "1500", "delta": "-0.20", "strikePrice": "580.00"},
                {"instrument": {"symbol": "SPY260509P00579000", "type": "OPTION"},
                 "outcome": "SUCCESS", "bid": "0.04", "ask": "0.05",
                 "openInterest": "1500", "delta": "-0.10", "strikePrice": "579.00"},
            ]})
        if path.endswith("/preflight/multi-leg"):
            return httpx.Response(200, json={"baseSymbol": "SPY", "legs": [],
                                              "orderValue": "60.00"})
        if path.endswith("/order/multileg"):
            return httpx.Response(200, json={"orderId": "x"})
        return httpx.Response(404)

    public = _make_public_client(handler)
    ed = EntryDetector(public, state, NullNotifier())
    d = ed.check_underlying(
        "SPY", _rsi_low_bars(), _market_ctx(), 10_000,
        datetime(2026, 5, 7, 10, 30, tzinfo=ET),
    )
    assert d.placed_order_id is None
    assert d.blocked_reason and "max_positions_per_underlying" in d.blocked_reason


def test_in_entry_window():
    public = _make_public_client(lambda r: httpx.Response(500))
    state_dummy = None  # not used in this method
    ed = EntryDetector(public, type("S", (), {"open_trades": lambda self: []})(), NullNotifier())
    assert ed.in_entry_window(datetime(2026, 5, 7, 10, 30, tzinfo=ET)) is True
    assert ed.in_entry_window(datetime(2026, 5, 7, 9, 59, tzinfo=ET)) is False
    assert ed.in_entry_window(datetime(2026, 5, 7, 11, 31, tzinfo=ET)) is False
