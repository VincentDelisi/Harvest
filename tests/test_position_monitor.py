"""Position monitor — fill reconciliation, profit target, stops, time stop, close P&L."""
from __future__ import annotations

import json
from datetime import datetime, time

import httpx
import pytest
import pytz

from engine.broker.auth import TokenManager
from engine.broker.public_client import PublicClient
from engine.notify.discord import NullNotifier
from engine.runtime.position_monitor import PositionMonitor
from engine.state.store import StateStore, TradeRecord

ET = pytz.timezone("America/New_York")


def _stub_token_manager() -> TokenManager:
    tm = TokenManager(secret="fake")
    tm._cached = type("T", (), {"token": "tok", "expires_at_epoch": 1e18})()
    return tm


def _client(handler) -> PublicClient:
    return PublicClient(
        account_id="acct-1",
        base_url="https://api.public.com",
        token_manager=_stub_token_manager(),
        dry_run=False,
        transport=httpx.MockTransport(handler),
    )


def _trade(state: StateStore, **overrides) -> TradeRecord:
    t = TradeRecord(
        trade_id=overrides.get("trade_id", "t1"),
        underlying=overrides.get("underlying", "SPY"),
        direction=overrides.get("direction", "PUT"),
        short_strike=overrides.get("short_strike", 580.0),
        long_strike=overrides.get("long_strike", 579.0),
        width=1.0,
        expiration=overrides.get("expiration", "2026-05-08"),
        short_symbol="SPY260508P00580000",
        long_symbol="SPY260508P00579000",
        quantity=overrides.get("quantity", 1),
        credit_received=overrides.get("credit_received", 0.40),
        open_order_id=overrides.get("open_order_id", "open-1"),
        open_status=overrides.get("open_status", "PENDING"),
        opened_at=overrides.get("opened_at", "2026-05-07T10:15:00-04:00"),
    )
    state.insert_trade(t)
    return t


@pytest.fixture
def state(tmp_path):
    s = StateStore(db_path=str(tmp_path / "p.db"))
    yield s
    s.close()


def test_reconcile_pending_to_filled_places_profit_target(state):
    """When PENDING order fills, monitor immediately places a 50%-profit close order."""
    _trade(state, trade_id="t1", credit_received=0.40, open_order_id="open-1")
    profit_orders: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if "/order/open-1" in path and req.method == "GET":
            return httpx.Response(200, json={
                "orderId": "open-1",
                "status": "FILLED",
                "averagePrice": "0.42",
            })
        if path.endswith("/preflight/multi-leg"):
            return httpx.Response(200, json={
                "baseSymbol": "SPY", "legs": [], "orderValue": "60.00",
            })
        if path.endswith("/order/multileg"):
            body = json.loads(req.content)
            profit_orders.append(body)
            return httpx.Response(200, json={"orderId": body["orderId"]})
        return httpx.Response(404, json={"path": path})

    public = _client(handler)
    monitor = PositionMonitor(public, state, NullNotifier())
    monitor.reconcile_pending(datetime(2026, 5, 7, 10, 30, tzinfo=ET))

    # Trade is now FILLED, with credit updated to averagePrice
    t = state.get_trade("t1")
    assert t.open_status == "FILLED"
    assert t.credit_received == 0.42

    # A profit-target close order was placed at GTC, debit ≈ (1 - 0.5) × 0.42 = 0.21
    assert len(profit_orders) == 1
    po = profit_orders[0]
    assert po["expiration"]["timeInForce"] == "GTC"
    assert po["limitPrice"] == "0.21"
    assert po["legs"][0]["openCloseIndicator"] == "CLOSE"


def test_check_exits_tested_strike_puts(state):
    """If underlying spot ≤ short put strike, hard close fires."""
    _trade(state, trade_id="t1", direction="PUT",
           short_strike=580.0, open_status="FILLED",
           open_order_id="open-1")

    closes: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path.endswith("/quotes"):
            body = json.loads(req.content)
            sym = body["instruments"][0]["symbol"]
            if sym == "SPY":
                return httpx.Response(200, json={"quotes": [
                    {"instrument": {"symbol": "SPY", "type": "EQUITY"},
                     "last": "579.50"},
                ]})
            # Spread mid quotes — return tight cheap
            return httpx.Response(200, json={"quotes": [
                {"instrument": {"symbol": "SPY260508P00580000", "type": "OPTION"},
                 "bid": "0.50", "ask": "0.55"},
                {"instrument": {"symbol": "SPY260508P00579000", "type": "OPTION"},
                 "bid": "0.10", "ask": "0.15"},
            ]})
        if path.endswith("/preflight/multi-leg"):
            return httpx.Response(200, json={
                "baseSymbol": "SPY", "legs": [], "orderValue": "1.00",
            })
        if path.endswith("/order/multileg"):
            body = json.loads(req.content)
            closes.append(body)
            return httpx.Response(200, json={"orderId": body["orderId"]})
        return httpx.Response(404)

    public = _client(handler)
    monitor = PositionMonitor(public, state, NullNotifier())
    monitor.check_exits(datetime(2026, 5, 7, 11, 0, tzinfo=ET))

    assert len(closes) == 1
    t = state.get_trade("t1")
    assert t.close_reason == "TESTED_STRIKE"
    assert t.close_status == "WORKING_STOP"


def test_check_exits_stop_loss_on_2x_credit(state):
    """If spread mid (debit to close) ≥ 3 × credit, stop fires (P&L = -2 × credit)."""
    _trade(state, trade_id="t1", credit_received=0.40,
           open_status="FILLED", short_strike=580.0)

    closes: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path.endswith("/quotes"):
            body = json.loads(req.content)
            sym = body["instruments"][0]["symbol"]
            if sym == "SPY":
                # Spot still safe from short strike
                return httpx.Response(200, json={"quotes": [
                    {"instrument": {"symbol": "SPY", "type": "EQUITY"},
                     "last": "583.00"},
                ]})
            # Spread mid = 1.30 (above 3 × 0.40 = 1.20)
            return httpx.Response(200, json={"quotes": [
                {"instrument": {"symbol": "SPY260508P00580000", "type": "OPTION"},
                 "bid": "1.40", "ask": "1.45"},
                {"instrument": {"symbol": "SPY260508P00579000", "type": "OPTION"},
                 "bid": "0.10", "ask": "0.12"},
            ]})
        if path.endswith("/preflight/multi-leg"):
            return httpx.Response(200, json={"baseSymbol": "SPY", "legs": [], "orderValue": "1.00"})
        if path.endswith("/order/multileg"):
            body = json.loads(req.content)
            closes.append(body)
            return httpx.Response(200, json={"orderId": body["orderId"]})
        return httpx.Response(404)

    public = _client(handler)
    monitor = PositionMonitor(public, state, NullNotifier())
    monitor.check_exits(datetime(2026, 5, 7, 11, 30, tzinfo=ET))

    assert len(closes) == 1
    t = state.get_trade("t1")
    assert t.close_reason == "STOP_LOSS"


def test_time_stop_fires_at_1525_on_expiration_day(state):
    _trade(state, trade_id="t1", expiration="2026-05-07",  # expires today
           open_status="FILLED", credit_received=0.40)

    closes: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path.endswith("/quotes"):
            body = json.loads(req.content)
            sym = body["instruments"][0]["symbol"]
            if sym == "SPY":
                return httpx.Response(200, json={"quotes": [
                    {"instrument": {"symbol": "SPY", "type": "EQUITY"},
                     "last": "583.00"},
                ]})
            # cheap mid — won't trigger stop loss
            return httpx.Response(200, json={"quotes": [
                {"instrument": {"symbol": "SPY260508P00580000", "type": "OPTION"},
                 "bid": "0.05", "ask": "0.07"},
                {"instrument": {"symbol": "SPY260508P00579000", "type": "OPTION"},
                 "bid": "0.01", "ask": "0.02"},
            ]})
        if path.endswith("/preflight/multi-leg"):
            return httpx.Response(200, json={"baseSymbol": "SPY", "legs": [], "orderValue": "1.00"})
        if path.endswith("/order/multileg"):
            body = json.loads(req.content)
            closes.append(body)
            return httpx.Response(200, json={"orderId": body["orderId"]})
        return httpx.Response(404)

    public = _client(handler)
    monitor = PositionMonitor(public, state, NullNotifier())
    # 15:25 ET on the expiration day
    monitor.check_exits(datetime(2026, 5, 7, 15, 25, tzinfo=ET))

    assert len(closes) == 1
    t = state.get_trade("t1")
    assert t.close_reason == "TIME_STOP"


def test_reconcile_closes_records_pnl(state):
    """When a working close order fills, P&L is computed and recorded."""
    _trade(state, trade_id="t1", credit_received=0.40, open_status="FILLED")
    # Manually plant a working close order
    state.conn.execute(
        "UPDATE trades SET close_order_id='close-1', close_status='WORKING_TARGET' WHERE trade_id='t1'"
    )
    state.conn.commit()

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if "/order/close-1" in path and req.method == "GET":
            return httpx.Response(200, json={
                "orderId": "close-1",
                "status": "FILLED",
                "averagePrice": "0.20",
            })
        return httpx.Response(404)

    public = _client(handler)
    monitor = PositionMonitor(public, state, NullNotifier())
    monitor.reconcile_closes(datetime(2026, 5, 7, 11, 0, tzinfo=ET))

    t = state.get_trade("t1")
    assert t.close_status == "FILLED"
    assert t.debit_paid == 0.20
    assert t.pnl == 20.0  # (0.40 - 0.20) * 100 * 1
    assert t.close_reason == "PROFIT_TARGET"
